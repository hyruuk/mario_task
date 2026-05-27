"""mario_task — NES Super Mario Bros experiment runner with EEG / iEEG markers."""

__version__ = "0.1.0"


def _ensure_retro_dll_path() -> None:
    # On Windows, _retro.pyd (built locally against MinGW + vcpkg by
    # setup_env.ps1) depends on libgcc_s_seh-1.dll / libstdc++-6.dll /
    # libwinpthread-1.dll / libz.dll which setup_env.ps1 copies into
    # the stable_retro package dir, and on the system python<ver>.dll.
    # Python 3.8+ narrowed extension-module DLL search to the .pyd's
    # directory + os.add_dll_directory paths, so we surface both.
    import os
    import sys
    if sys.platform != "win32":
        return
    import site
    for sp in site.getsitepackages():
        cand = os.path.join(sp, "stable_retro")
        if os.path.isdir(cand):
            try:
                os.add_dll_directory(cand)
            except OSError:
                pass
    for prefix in (sys.prefix, sys.base_prefix):
        if os.path.isdir(prefix):
            try:
                os.add_dll_directory(prefix)
            except OSError:
                pass


_ensure_retro_dll_path()
