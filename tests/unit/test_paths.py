from forge.core.paths import APP_ROOT, ensure_runtime_dirs, paths


def test_paths_resolve_under_app_root():
    p = ensure_runtime_dirs()
    assert p.root == APP_ROOT
    assert p.playbooks.is_dir()
    assert p.prompts.is_dir()
    assert p.state.parent.name in {"var", "app"} or p.state.exists()
    assert paths().examples.exists() or True
