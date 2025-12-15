# -*- mode: python ; coding: utf-8 -*-
"""
VoiceType PyInstaller Spec File
===============================
Builds VoiceType as a portable EXE application.

Usage:
    pyinstaller voicetype.spec --clean

Output:
    dist/VoiceType/ - Portable application folder
"""

import sys
import os
from pathlib import Path

# Force PySide6 as the Qt binding (avoids PyQt5 conflict)
os.environ['QT_API'] = 'pyside6'

block_cipher = None

# Project root
PROJECT_ROOT = Path(SPECPATH)

# Collect data files
datas = [
    # Config files
    (str(PROJECT_ROOT / 'config'), 'config'),
    # Progress IPC module
    (str(PROJECT_ROOT / 'progress_ipc.py'), '.'),
]

# Add FunASR version.txt (required at runtime)
import site
for sp in site.getsitepackages() + [site.getusersitepackages()]:
    funasr_version = Path(sp) / 'funasr' / 'version.txt'
    if funasr_version.exists():
        datas.append((str(funasr_version), 'funasr'))
        print(f"[Spec] Added FunASR version.txt from {funasr_version}")
        break

# Collect all Python source files as data (for dynamic imports)
# This ensures the voicetype package structure is preserved
for subdir in ['core', 'ui', 'features', 'scheduler']:
    src_path = PROJECT_ROOT / subdir
    if src_path.exists():
        datas.append((str(src_path), subdir))

# Hidden imports that PyInstaller might miss
hiddenimports = [
    # PySide6 core
    'PySide6.QtCore',
    'PySide6.QtWidgets',
    'PySide6.QtGui',

    # Audio
    'pyaudio',
    'sounddevice',
    'soundfile',

    # Silero-VAD and ONNX
    'silero_vad',
    'onnxruntime',

    # Chinese NLP
    'pypinyin',
    'pypinyin.contrib',
    'pypinyin.style',

    # Torch and FunASR (heavy dependencies)
    'torch',
    'torchaudio',
    'funasr',
    'modelscope',

    # Other
    'keyboard',
    'pyperclip',
    'win32clipboard',
    'win32gui',
    'win32con',
    'win32api',
    'ctypes',
    'ctypes.wintypes',

    # For HTTPS requests
    'urllib3',
    'certifi',
    'ssl',
]

# Excludes to reduce size and speed up build - AGGRESSIVE exclusions
excludes = [
    # === Qt Binding Conflicts ===
    'PyQt5', 'PyQt5.QtCore', 'PyQt5.QtGui', 'PyQt5.QtWidgets', 'PyQt5.sip',
    'qtpy',  # Qt abstraction layer

    # === Unused PySide6 modules (saves ~200MB) ===
    'PySide6.QtDesigner', 'PySide6.QtHelp', 'PySide6.QtNetwork',
    'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets',
    'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtPositioning',
    'PySide6.QtPrintSupport', 'PySide6.QtQml', 'PySide6.QtQuick',
    'PySide6.QtQuickWidgets', 'PySide6.QtRemoteObjects', 'PySide6.QtScxml',
    'PySide6.QtSensors', 'PySide6.QtSerialPort', 'PySide6.QtSql',
    'PySide6.QtSvg', 'PySide6.QtSvgWidgets', 'PySide6.QtTest',
    'PySide6.QtUiTools', 'PySide6.QtWebChannel', 'PySide6.QtWebEngine',
    'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
    'PySide6.QtWebSockets', 'PySide6.QtXml',
    'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DInput',
    'PySide6.Qt3DLogic', 'PySide6.Qt3DExtras', 'PySide6.Qt3DAnimation',
    'PySide6.QtCharts', 'PySide6.QtDataVisualization',
    'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets',
    'PySide6.QtBluetooth', 'PySide6.QtNfc',

    # === Development & Testing ===
    'pytest', 'pip', 'setuptools', 'wheel', 'sphinx', 'docutils',
    'unittest', 'test', 'tests',

    # === Deep Learning (not FunASR) ===
    'tensorflow', 'tensorboard', 'keras',
    'transformers',  # Not needed, use FunASR directly
    'diffusers', 'accelerate',
    'bitsandbytes',  # Quantization, not needed
    'timm',  # PyTorch image models

    # === Image Processing (not needed) ===
    'skimage', 'scikit-image',
    'PIL.ImageFilter', 'PIL.ImageEnhance',
    'imageio', 'imageio_ffmpeg',
    'pywt',  # Wavelets
    'pymatting',
    'cv2',  # OpenCV - not needed for voice

    # === 3D / Graphics (not needed) ===
    'trimesh', 'shapely', 'rtree',
    'pyglet', 'glfw', 'OpenGL',
    'pyvista', 'vtk',

    # === NLP (use pypinyin only) ===
    'nltk', 'spacy', 'gensim',
    'jieba',  # Chinese segmentation, pypinyin enough

    # === Data Science (not needed) ===
    'matplotlib', 'seaborn',
    'pandas',  # Not needed for voice input
    'pyarrow', 'fastparquet',
    'openpyxl', 'xlrd', 'xlwt', 'tables',
    'statsmodels', 'patsy',
    'xarray', 'intake',
    'dask', 'distributed',

    # === Machine Learning (sklearn partial) ===
    'sklearn.ensemble', 'sklearn.svm', 'sklearn.tree',
    'sklearn.neural_network', 'sklearn.decomposition',
    'sklearn.manifold', 'sklearn.gaussian_process',

    # === Visualization ===
    'bokeh', 'plotly', 'altair', 'panel', 'holoviews',
    'IPython', 'jupyter', 'notebook', 'ipywidgets',

    # === Astronomy ===
    'astropy', 'skyfield',

    # === Browser / Web ===
    'selenium', 'playwright', 'pyppeteer',
    'flask', 'django', 'fastapi', 'starlette',
    'uvicorn', 'gunicorn', 'uvloop',

    # === Database ===
    'MySQLdb', 'psycopg2', 'sqlalchemy', 'sqlite3',
    'pymongo', 'redis',

    # === Cloud / AWS ===
    'boto3', 'botocore', 'azure', 'google.cloud',

    # === Networking ===
    'paramiko', 'fabric', 'invoke',
    'websockets', 'aiohttp',

    # === Config / Misc ===
    'hydra', 'omegaconf',
    'sentry_sdk',
    'emoji',
    'umap',

    # === GUI (not PySide6) ===
    'tkinter', '_tkinter',
    'wx', 'wxPython',

    # === Other heavy packages ===
    'gevent', 'greenlet',
    'numba', 'llvmlite',  # JIT - not needed
    'sympy',  # Symbolic math
    'networkx',  # Graphs
    'igraph',

    # === Compression ===
    'lz4', 'zstd', 'blosc',

    # === Crypto (keep only what's needed) ===
    'nacl', 'bcrypt',
]

a = Analysis(
    ['launcher.py'],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Filter out unnecessary files to reduce size
def filter_binaries(binaries):
    """Remove unnecessary DLLs and binaries."""
    excludes = [
        'Qt6WebEngine',
        'Qt6Pdf',
        'Qt6Quick',
        'Qt6Qml',
        'Qt6Designer',
        'opengl32sw',
        'libGLESv2',
        'd3dcompiler',
    ]
    return [b for b in binaries if not any(ex in b[0] for ex in excludes)]

a.binaries = filter_binaries(a.binaries)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VoiceType',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # No console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / 'assets' / 'icon.ico') if (PROJECT_ROOT / 'assets' / 'icon.ico').exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VoiceType',
)

# Post-build fix: Ensure base_library.zip is copied to dist
# (Workaround for PyInstaller bug where base_library.zip may be missing)
import shutil
base_lib_src = PROJECT_ROOT / 'build' / 'voicetype' / 'base_library.zip'
base_lib_dst = PROJECT_ROOT / 'dist' / 'VoiceType' / '_internal' / 'base_library.zip'
if base_lib_src.exists() and not base_lib_dst.exists():
    base_lib_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(base_lib_src), str(base_lib_dst))
    print(f"[Post-build] Copied base_library.zip to {base_lib_dst}")
