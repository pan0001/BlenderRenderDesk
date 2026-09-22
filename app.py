from renderdesk.app import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        import os
        import sys
        if os.name == 'nt' and getattr(sys, 'frozen', False):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(error), 'Render Desk 无法启动', 0x10)
        raise
