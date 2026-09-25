import sys


def main() -> None:
    from piceli.tempfiles import install_signal_cleanup

    # SIGTERM/SIGHUP remove live temporary directories (TLS material, OCI
    # layouts, worktrees) before the default action ends the process.
    install_signal_cleanup()
    if len(sys.argv) > 1 and sys.argv[1] == "artifacts":
        from piceli.artifacts.cli import main as artifacts_main

        raise SystemExit(artifacts_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--help-json":
        sys.argv[1:2] = ["help-json"]
    from piceli.k8s.cli import app as k8s_app

    k8s_app()


if __name__ == "__main__":
    main()
