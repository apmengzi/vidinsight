# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['uvicorn', 'fastapi', 'starlette']
hiddenimports += collect_submodules('chromadb')


a = Analysis(
    ['main.py'],
    pathex=['backend'],
    binaries=[],
    datas=[('D:\\CyberStomach\\VidInsight\\backend\\app\\db\\builtin_providers.json', '.'), ('D:\\CyberStomach\\VidInsight\\.venv\\Lib\\site-packages\\nvidia\\cublas\\bin', 'nvidia\\cublas\\bin'), ('D:\\CyberStomach\\VidInsight\\.venv\\Lib\\site-packages\\nvidia\\cudnn\\bin', 'nvidia\\cudnn\\bin'), ('D:\\CyberStomach\\VidInsight\\.venv\\Lib\\site-packages\\nvidia\\cuda_nvrtc\\bin', 'nvidia\\cuda_nvrtc\\bin'), ('D:\\CyberStomach\\VidInsight\\.venv\\Lib\\site-packages\\faster_whisper\\assets', 'faster_whisper\\assets')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VidInsightBackend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VidInsightBackend',
)
