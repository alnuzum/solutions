"""The pages of a scan drawn as JPEGs in a process of its own (``python -I -m alnuzum_orders.render``): the
drawing is shared with the other solutions, ``alnuzum_files.render``."""

import sys

from alnuzum_files.render import draw, main

__all__ = ["draw", "main"]

if __name__ == "__main__":
    sys.exit(main(sys.argv))
