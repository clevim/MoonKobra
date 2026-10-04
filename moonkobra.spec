# Spec do PyInstaller para o moonkobra (Linux e Windows).
# Roda a partir da raiz do repositório: `pyinstaller moonkobra.spec`.
#
# Embute o tema web (web/) no binário único, lido em tempo de execução por
# sys._MEIPASS (_WEB_BASE no bridge).
from PyInstaller.utils.hooks import collect_all

# Só o arquivo estático de data/: o resto da pasta (banco, session.key, G-codes,
# logs) é dado de quem rodou o bridge nesta máquina e não pode ir no binário.
datas = [("web", "web"), ("data/orca_filaments.json", "static"), ("VERSION", ".")]
binaries = []
hiddenimports = []

# pycryptodome inteiro (criptografia da autenticação com a impressora)
_d, _b, _h = collect_all("pycryptodome")
datas += _d
binaries += _b
hiddenimports += _h

a = Analysis(
    ["kobrax_moonraker_bridge.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="moonkobra",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    onefile=True,
)
