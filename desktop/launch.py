"""Entry point for the portable Windows launcher."""
import ctypes
import os
import sys
import traceback
from pathlib import Path

base = Path(__file__).resolve().parent.parent
(base / 'logs').mkdir(exist_ok=True)
sys.stdout = sys.stderr = (base / 'logs' / 'reader.log').open('a', encoding='utf-8', buffering=1)
os.environ['HF_HOME'] = str(base / 'cache' / 'hf')
os.environ['HF_MODULES_CACHE'] = str(base / 'cache' / 'modules')
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
try:
    if '--api' in sys.argv:
        import runpy
        sys.argv.remove('--api')
        runpy.run_module('local_api',run_name='__main__')
        sys.exit(0)
    from main import main
    sys.exit(main())
except Exception:
    traceback.print_exc()
    ctypes.windll.user32.MessageBoxW(None, 'Не удалось запустить приложение. Подробности: ' + str(base / 'logs' / 'reader.log'), 'RedReaderStudio', 0x10)
    sys.exit(1)
