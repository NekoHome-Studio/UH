#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod kernel;

use std::{env, error::Error, io};

use kernel::{Kernel, DESKTOP_URL, WEB_URL};
use serde::Deserialize;
use tauri::{image::Image, menu::MenuBuilder, tray::TrayIconBuilder, App, AppHandle, Manager,
    RunEvent, WebviewWindow, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_opener::OpenerExt;

const WINDOW: &str = "main";
const TRAY: &str = "uh-status";

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum Status {
    Idle,
    Running,
    Degraded,
}

impl Status {
    fn icon(&self) -> Image<'static> {
        let rgb = match self {
            Self::Idle => [96, 165, 250],
            Self::Running => [52, 211, 153],
            Self::Degraded => [251, 191, 36],
        };
        let mut rgba = Vec::with_capacity(24 * 24 * 4);
        for y in 0..24 {
            for x in 0..24 {
                let dx = 2 * x - 23;
                let dy = 2 * y - 23;
                let alpha = if dx * dx + dy * dy <= 400 { 255 } else { 0 };
                rgba.extend_from_slice(&[rgb[0], rgb[1], rgb[2], alpha]);
            }
        }
        Image::new_owned(rgba, 24, 24)
    }

    fn tooltip(&self) -> &'static str {
        match self {
            Self::Idle => "UH! · 空闲",
            Self::Running => "UH! · 运行中",
            Self::Degraded => "UH! · 降级",
        }
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum Notice {
    Finished,
    Failed,
}

#[tauri::command]
fn report_task_state(window: WebviewWindow, status: Status, notice: Option<Notice>) -> Result<(), String> {
    if window.label() != WINDOW || window.url().map_err(|e| e.to_string())?.as_str() != DESKTOP_URL {
        return Err("不支持的桌面页面".into());
    }
    let app = window.app_handle();
    if let Some(tray) = app.tray_by_id(TRAY) {
        tray.set_icon(Some(status.icon())).map_err(|e| e.to_string())?;
        #[cfg(not(target_os = "linux"))]
        tray.set_tooltip(Some(status.tooltip())).map_err(|e| e.to_string())?;
    }
    if let Some(notice) = notice {
        let (title, body) = match notice {
            Notice::Finished => ("UH! 任务完成", "打开 UH! 查看回复。"),
            Notice::Failed => ("UH! 任务失败", "打开 UH! 查看错误详情。"),
        };
        app.notification().builder().title(title).body(body).show().map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn show_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(WINDOW) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn show_error(app: &AppHandle, error: &str) {
    app.dialog().message(error).title("UH! Desktop").kind(MessageDialogKind::Error).show(|_| {});
}

fn setup(app: &mut App) -> Result<(), Box<dyn Error>> {
    app.state::<Kernel>().start(app.handle())?;
    let config = app.config().app.windows.iter().find(|window| window.label == WINDOW)
        .ok_or_else(|| io::Error::other("缺少 Desktop 主窗口配置。"))?;
    let window = WebviewWindowBuilder::from_config(app, config)?
        .on_navigation(|url| url.as_str() == DESKTOP_URL)
        .build()?;

    let menu = MenuBuilder::new(app)
        .text("show", "显示窗口")
        .text("hide", "隐藏窗口")
        .text("open-web", "打开 Web UI")
        .separator()
        .text("quit", "退出")
        .build()?;
    TrayIconBuilder::with_id(TRAY)
        .icon(Status::Idle.icon())
        .tooltip(Status::Idle.tooltip())
        .menu(&menu)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "show" => show_window(app),
            "hide" => {
                if let Some(window) = app.get_webview_window(WINDOW) {
                    let _ = window.hide();
                }
            }
            "open-web" => {
                if let Err(error) = app.opener().open_url(WEB_URL, None::<&str>) {
                    show_error(app, &error.to_string());
                }
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)?;

    let handle = app.handle().clone();
    window.on_window_event(move |event| {
        if let WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();
            if let Some(window) = handle.get_webview_window(WINDOW) {
                let _ = window.hide();
            }
        }
    });

    let configured = app.config().plugins.0.get("uh-desktop")
        .and_then(|config| config.get("wakeShortcut"))
        .and_then(|shortcut| shortcut.as_str())
        .ok_or_else(|| io::Error::other("缺少 Desktop 快捷键配置。"))?;
    let shortcut = env::var("UH_DESKTOP_SHORTCUT").unwrap_or_else(|_| configured.to_owned());
    if let Err(error) = app.global_shortcut().on_shortcut(shortcut.as_str(), |app, _, event| {
        if event.state == ShortcutState::Pressed {
            show_window(app);
        }
    }) {
        show_error(app.handle(), &format!("快捷键 {shortcut} 注册失败：{error}。可通过托盘打开窗口。"));
    }
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| show_window(app)))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .manage(Kernel::default())
        .invoke_handler(tauri::generate_handler![report_task_state])
        .setup(|app| {
            if let Err(error) = setup(app) {
                app.state::<Kernel>().stop();
                let handle = app.handle().clone();
                app.dialog().message(error.to_string()).title("UH! Desktop 无法启动")
                    .kind(MessageDialogKind::Error).show(move |_| handle.exit(1));
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("could not build UH! Desktop")
        .run(|app, event| {
            if matches!(event, RunEvent::Exit) {
                app.state::<Kernel>().stop();
            }
        });
}
