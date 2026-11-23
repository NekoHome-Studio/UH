use std::{
    env, fs,
    io,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};

use reqwest::{blocking::Client, redirect::Policy};
use tauri::{path::BaseDirectory, AppHandle, Manager};

pub const WEB_URL: &str = "http://127.0.0.1:8080/";
pub const DESKTOP_URL: &str = "http://127.0.0.1:8080/?desktop=1";

#[derive(Default)]
pub struct Kernel(Mutex<Option<Child>>);

impl Kernel {
    pub fn start(&self, app: &AppHandle) -> Result<(), Box<dyn std::error::Error>> {
        let http = Client::builder()
            .no_proxy()
            .redirect(Policy::none())
            .timeout(Duration::from_millis(500))
            .build()?;
        if probe(&http)? {
            return Ok(());
        }

        let (source, workdir) = runtime(app)?;
        let state_dir = workdir.join(".uh");
        fs::create_dir_all(&state_dir)?;
        let log_path = state_dir.join("desktop-kernel.log");
        let log = fs::OpenOptions::new().create(true).append(true).open(&log_path)?;
        let mut command = Command::new(python(&source)?);
        command
            .args(["-m", "uvicorn", "kernel.main:app", "--app-dir"])
            .arg(source)
            .args(["--host", "127.0.0.1", "--port", "8080", "--workers", "1", "--no-access-log"])
            .current_dir(workdir)
            .stdin(Stdio::null())
            .stdout(log.try_clone()?)
            .stderr(log);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x08000000;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        let mut owned = self.0.lock().unwrap();
        let child = owned.insert(command.spawn().map_err(|e| {
            io::Error::new(e.kind(), format!("无法启动 Python 内核：{e}。请检查 UH_DESKTOP_PYTHON。"))
        })?);
        let deadline = Instant::now() + Duration::from_secs(15);
        loop {
            if let Some(status) = child.try_wait()? {
                return Err(io::Error::other(format!(
                    "内核启动失败（{status}）。启动日志位于 {}", log_path.display()
                )).into());
            }
            if probe(&http)? {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(io::Error::other(format!(
                    "内核在 15 秒内未就绪。启动日志位于 {}", log_path.display()
                )).into());
            }
            thread::sleep(Duration::from_millis(100));
        }
    }

    pub fn stop(&self) {
        if let Some(mut child) = self.0.lock().unwrap().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn probe(http: &Client) -> io::Result<bool> {
    match http.get(DESKTOP_URL).send() {
        Ok(response)
            if response.status().is_success()
                && response.headers().get("x-uh-desktop").is_some_and(|value| value == "1") =>
        {
            Ok(true)
        }
        Err(error) if error.is_connect() => Ok(false),
        _ => Err(io::Error::new(
            io::ErrorKind::AddrInUse,
            "127.0.0.1:8080 已被占用或未响应。请释放端口，或启动支持 Desktop 的 UH! 内核。",
        )),
    }
}

fn has_runtime(root: &Path) -> bool {
    root.join("kernel/main.py").is_file() && root.join("ui/index.html").is_file()
}

fn runtime(app: &AppHandle) -> Result<(PathBuf, PathBuf), Box<dyn std::error::Error>> {
    if let Some(path) = env::var_os("UH_DESKTOP_ROOT") {
        let root = PathBuf::from(path);
        if !root.is_absolute() || !has_runtime(&root) {
            return Err(io::Error::other("UH_DESKTOP_ROOT 必须为 UH! 项目根目录的绝对路径。").into());
        }
        return Ok((root.clone(), root));
    }
    if let Some(root) = env::current_dir()?.ancestors().find(|root| has_runtime(root)) {
        return Ok((root.to_owned(), root.to_owned()));
    }
    if let Ok(bundled) = app.path().resolve("runtime", BaseDirectory::Resource) {
        if has_runtime(&bundled) {
            return Ok((bundled, app.path().app_local_data_dir()?));
        }
    }
    Err(io::Error::other("找不到 UH! 内核源码。请从仓库启动或设置 UH_DESKTOP_ROOT。").into())
}

fn python(root: &Path) -> io::Result<PathBuf> {
    if let Some(path) = env::var_os("UH_DESKTOP_PYTHON") {
        let path = PathBuf::from(path);
        return if path.is_absolute() && path.is_file() {
            Ok(path)
        } else {
            Err(io::Error::other("UH_DESKTOP_PYTHON 必须为 Python 可执行文件的绝对路径。"))
        };
    }
    let (local, on_path) = if cfg!(windows) {
        (".venv/Scripts/python.exe", "python.exe")
    } else {
        (".venv/bin/python", "python3")
    };
    let local = root.join(local);
    Ok(if local.is_file() { local } else { PathBuf::from(on_path) })
}
