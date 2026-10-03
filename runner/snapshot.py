"""Save a complete report tool without external Python dependencies."""
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def save_report_tool(output):
    files={'__main__.py':b'from benchmark import main\nimport sys\nsys.exit(main())\n',
           'benchmark.py':(ROOT/'benchmark.py').read_bytes()}
    files.update({str(path.relative_to(ROOT)):path.read_bytes() for path in sorted((ROOT/'runner').glob('*.py'))})
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(files.items()):
            info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,data)
