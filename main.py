"""MC Manager - Python Edition. Entry point."""
import sys
import traceback


def main() -> None:
    try:
        from mcmanager.ui.app import launch
        launch()
    except Exception:
        traceback.print_exc()
        # Keep the console window open on Windows so the error is readable.
        try:
            input("\n[An error occurred. Press Enter to exit...]")
        except EOFError:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
