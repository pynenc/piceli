from piceli.k8s.cli import app as k8s_app


def main() -> None:
    # so far directly only k8s client is implemented
    k8s_app()


if __name__ == "__main__":
    main()
