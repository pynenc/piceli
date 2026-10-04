import sys


def main() -> None:
    # First, before any other import: in the controller and UI images, verify
    # the installed files against the build's manifest (PICELI_SELF_CHECK).
    from piceli.integrity import self_check

    self_check()
    from piceli.tempfiles import install_signal_cleanup

    # SIGTERM/SIGHUP remove live temporary directories (TLS material, OCI
    # layouts, worktrees) before the default action ends the process.
    install_signal_cleanup()
    if len(sys.argv) > 1 and sys.argv[1] == "artifacts":
        from piceli.artifacts.cli import main as artifacts_main
        from piceli.k8s.cli.profiles import expand_profile_argv

        raise SystemExit(
            artifacts_main(expand_profile_argv(sys.argv[2:], artifacts=True))
        )
    if len(sys.argv) > 1 and sys.argv[1] == "--help-json":
        sys.argv[1:2] = ["help-json"]
    from piceli.k8s.cli import app as k8s_app
    from piceli.k8s.cli.profiles import main_argv

    k8s_app(args=main_argv())


if __name__ == "__main__":
    main()
