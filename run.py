import sys
import uvicorn

COMMANDS = {
    "create-superadmin": "app.cli:create_superadmin",
}

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in COMMANDS:
        # Dispatch to CLI command
        module_path, func_name = COMMANDS[sys.argv[1]].rsplit(":", 1)
        import importlib
        module = importlib.import_module(module_path)
        getattr(module, func_name)()
    else:
        import os
        dev = os.environ.get("WAM_DEV", "").lower() in ("1", "true", "yes")
        uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=dev)
