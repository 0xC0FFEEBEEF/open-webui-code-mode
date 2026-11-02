import sys
from pathlib import Path

def main(argv):
    for p in map(Path, argv):
        print(p.name, len(p.read_text()))
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
