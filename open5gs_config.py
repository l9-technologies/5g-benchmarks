#!/usr/bin/env python3
"""Keep the original Open5GS configuration command and Python imports."""
from adapters.open5gs_config import (ROOT_DIR,DEFAULT_PROFILE,parse_args,required_text,
    write_config,configure,main)

if __name__=='__main__':raise SystemExit(main())
