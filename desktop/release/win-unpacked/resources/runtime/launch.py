"""Entrypoint for the bundled sources (no machine-global Python required)."""
import os
import runpy
import sys

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
mode = sys.argv.pop(1)
if mode == 'acp':
    from acp_adapter.entry import main
    main()
elif mode == 'gateway':
    runpy.run_module('bucker.desktop_gateway', run_name='__main__')
else:
    raise SystemExit('Unknown runtime mode')
