import os

from command_interface import main

raise SystemExit(main(prog=os.environ.get("DISPATCH_PROGRAM_NAME", "dispatch-core")))
