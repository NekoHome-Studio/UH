fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&["report_task_state"]),
        ),
    )
    .expect("could not build UH! Desktop configuration");
}
