import importlib.util
import sys
import threading
import tkinter as tk
from tkinter import messagebox
from typing import TYPE_CHECKING, Optional, Type

from EasyFlex.config import AppConfig, load_config, setup_logging

if TYPE_CHECKING:
    from EasyFlex.extractor import InvoiceExtractor
    from EasyFlex.gui import AppGUI


_CORE_DEPENDENCIES = {
    "requests": "HTTP komunikace (ABRA import)",
    "pandas": "Práce s tabulkami a CSV",
    "pdf2image": "Konverze PDF → obrázky",
    "PIL": "Zpracování obrázků",
    "openai": "Extrakce faktur pomocí AI",
    "charset_normalizer": "Detekce kódování CSV",
}


def _recommended_pip_command() -> str:
    exe = sys.executable or "python"
    if " " in exe:
        exe = f'"{exe}"'
    return f"{exe} -m pip install -r EasyFlex/requirements.txt"


def _format_missing_dependency(exc: ModuleNotFoundError) -> str:
    missing = exc.name or str(exc)
    return (
        f"Chybí Python knihovna '{missing}'.\n\n"
        "Zřejmě běžíte jiného Pythona, než kam jste knihovny instalovali. "
        f"Spusťte prosím instalaci tímto přesným příkazem:\n{_recommended_pip_command()}"
    )


def _handle_missing_dependency(root: tk.Tk, exc: ModuleNotFoundError) -> None:
    messagebox.showerror("EasyFlex", _format_missing_dependency(exc))


def _import_app_gui(root: tk.Tk) -> Optional[Type["AppGUI"]]:
    try:
        from EasyFlex.gui import AppGUI  # noqa: WPS433 (runtime import for graceful failure)
        return AppGUI
    except ModuleNotFoundError as exc:
        _handle_missing_dependency(root, exc)
        return None


def _check_core_dependencies(root: tk.Tk) -> bool:
    missing = []
    for module_name in _CORE_DEPENDENCIES:
        if importlib.util.find_spec(module_name) is None:
            missing.append(module_name)
    if not missing:
        return True

    details = "\n".join(
        f"• {name} – {_CORE_DEPENDENCIES[name]}" for name in missing
    )
    msg = (
        "Některé důležité knihovny chybí:\n"
        f"{details}\n\n"
        "Instalace pomocí samotného 'pip' často cílí na jinou instalaci Pythonu.\n"
        "Použijte prosím příkaz:\n"
        f"{_recommended_pip_command()}"
    )
    messagebox.showerror("EasyFlex", msg)
    root.destroy()
    return False


def _build_extractor(cfg: AppConfig) -> "InvoiceExtractor":
    from EasyFlex.extractor import InvoiceExtractor  # Lazy import to speed initial start
    return InvoiceExtractor(config=cfg)


def main() -> None:
    setup_logging()
    cfg = load_config()
    root = tk.Tk()
    root.withdraw()
    if not _check_core_dependencies(root):
        return
    AppGUI_cls = _import_app_gui(root)
    if AppGUI_cls is None:
        root.destroy()
        return
    if not cfg.openai_api_key:
        messagebox.showwarning(
            "API klíč není nastaven",
            "Nenalezen API klíč. Aplikace se otevře, ale extrakce PDF bude zakázána – zadejte klíč v Nastavení (záložka Extractor).",
        )
    root.deiconify()
    app = AppGUI_cls(root)
    app.status_var.set("Načítám moduly…")

    def init_extractor() -> None:
        try:
            extractor = _build_extractor(cfg)
        except ModuleNotFoundError as exc:
            app.notify_extractor_error(RuntimeError(_format_missing_dependency(exc)))
        except Exception as exc:  # noqa: BLE001
            app.notify_extractor_error(exc)
        else:
            app.set_extractor(extractor)

    threading.Thread(target=init_extractor, daemon=True).start()
    root.mainloop()


if __name__ == "__main__":
    main()
