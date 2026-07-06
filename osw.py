from osw.deps import check_deps

check_deps()

from osw.cli import app

if __name__ == "__main__":
    app()
