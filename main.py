def main():
    import runpy
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root / "data"))
    runpy.run_path(str(root / "data" / "generate_annual_run.py"), run_name="__main__")


if __name__ == "__main__":
    main()
