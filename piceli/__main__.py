import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "artifacts":
        from piceli.artifacts.cli import main as artifacts_main

        raise SystemExit(artifacts_main(sys.argv[2:]))
    from piceli.k8s.cli import app as k8s_app

    k8s_app()


if __name__ == "__main__":
    main()
