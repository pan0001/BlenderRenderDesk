"""Restart the original scheduler in its Python environment and record its exit."""
from pathlib import Path
import runpy
import sys
import time
import traceback
from protocol import read, write

root=Path(sys.argv[1]).resolve()
plan=read(root/'job.json')['batch']
code=0
try:
    sys.argv=list(plan['args'][1:])
    sys.path.insert(0,str(Path(plan['script']).parent))
    runpy.run_path(plan['script'],run_name='__main__')
except SystemExit as error:
    code=error.code if isinstance(error.code,int) else 0 if error.code is None else 1
except BaseException:
    traceback.print_exc()
    code=1
finally:
    write(root/'batch-result.json',{'exit_code':code,'updated':time.time()})
raise SystemExit(code)
