"""`python -m oselia_provision` entry point (mirrors the `oselia` console script)."""
from .cli import app

if __name__ == "__main__":
    app()
