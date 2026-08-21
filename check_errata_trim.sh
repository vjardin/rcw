#!/bin/sh
# Copyright Free Mobile, 2026, Vincent Jardin
#
# Check what the protocol-gated errata actually changed.
#
# So build the tree twice:
#
#   no-trim  RCW_ERRATA_NO_TRIM defined, every conditional block enabled.
#            This is what the board would emit if it declared no protocol
#            at all, and it is the build that must not lose anything.
#   trimmed  the normal build.
#
# then check
#
#   trimmed  <=  reference  <=  no-trim
#
# usage: ./check_errata_trim.sh [reference-rev]     (default: origin/devel)

set -eu

REF=${1:-origin/devel}
TMP=$(mktemp -d)
trap 'git worktree remove --force "$TMP/ref" 2>/dev/null || true; rm -rf "$TMP"' EXIT

command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
git rev-parse --verify -q "$REF^{commit}" >/dev/null ||
	{ echo "no such revision: $REF" >&2; exit 1; }

echo "reference : $REF ($(git rev-parse --short "$REF"))"
echo "under test: $(git rev-parse --short HEAD)"
echo

echo "building the reference..."
git worktree add -q --detach "$TMP/ref" "$REF"
make -s -C "$TMP/ref" >/dev/null

echo "building with the trimming off..."
make -s clean >/dev/null 2>&1 || true
make -s RCW_CPPFLAGS=-DRCW_ERRATA_NO_TRIM >/dev/null
find . -name '*.bin' -o -name '*.bin.swapped' | while read -r f; do
	mkdir -p "$TMP/notrim/$(dirname "$f")"
	cp "$f" "$TMP/notrim/$f"
done

echo "building normally..."
make -s clean >/dev/null 2>&1 || true
make -s >/dev/null
echo

REF_DIR=$TMP/ref NOTRIM_DIR=$TMP/notrim python3 - "$REF" <<'PY'
import os, subprocess, sys, collections, glob

ref_dir, notrim_dir = os.environ['REF_DIR'], os.environ['NOTRIM_DIR']
CMDS = ('write', 'awrite', 'blockcopy', 'loadacwindow', 'poll', 'wait')

def commands(root, src, extra=()):
    """The PBI command stream of one source, as the preprocessor leaves it."""
    path = os.path.abspath(os.path.join(root, src))
    p = subprocess.run(['gcc', '-E', '-x', 'c', '-P', '-I', '..'] +
                       list(extra) + [path],
                       cwd=os.path.dirname(path), capture_output=True, text=True)
    if p.returncode:
        return None
    out = []
    for line in p.stdout.splitlines():
        t = ' '.join(line.split())
        if t.split(' ')[0].split('.')[0] in CMDS:
            out.append(t)
    return out

identical = reordered = gained = lost = failed = skipped = invented = 0
size_ref = size_new = 0
for ref_bin in sorted(glob.glob(ref_dir + '/*/*/*.bin*')):
    rel = os.path.relpath(ref_bin, ref_dir)
    src = rel.split('.bin')[0] + '.rcw'
    if not os.path.exists(src):
        skipped += 1          # board added upstream after this branch point
        continue
    if not os.path.exists(os.path.join(notrim_dir, rel)):
        print('MISSING  %s in the no-trim build' % rel)
        failed += 1
        continue

    ref_bytes = open(ref_bin, 'rb').read()
    size_ref += len(ref_bytes)
    size_new += os.path.getsize(rel)

    a = commands(ref_dir, src)                          # reference
    tr = commands('.', src)                             # normal build
    if a is None or tr is None:
        print('CPP FAIL %s' % src)
        failed += 1
        continue
    invented_here = collections.Counter(tr) - collections.Counter(a)
    if invented_here:
        print('INVENTED %s: the trimmed build has %d command(s) the '
              'reference never had, e.g. %s'
              % (src, sum(invented_here.values()), next(iter(invented_here))))
        invented += 1

    if open(os.path.join(notrim_dir, rel), 'rb').read() == ref_bytes:
        identical += 1
        continue

    nt = commands('.', src, ['-DRCW_ERRATA_NO_TRIM'])   # trimming off
    if nt is None:
        print('CPP FAIL %s' % src)
        failed += 1
        continue
    missing = collections.Counter(a) - collections.Counter(nt)
    extra = collections.Counter(nt) - collections.Counter(a)
    if missing:
        print('LOST     %s: %d command(s) the reference had, e.g. %s'
              % (src, sum(missing.values()), next(iter(missing))))
        lost += 1
    elif extra:
        print('GAINED   %s: %d command(s) not in the reference, e.g. %s'
              % (src, sum(extra.values()), next(iter(extra))))
        gained += 1
    else:
        reordered += 1

print()
print('no-trim build vs %s:' % sys.argv[1])
print('  byte-identical                     : %d' % identical)
print('  same commands, emitted in new order: %d' % reordered)
print('  gained a command                   : %d' % gained)
print('  LOST a command                     : %d' % lost)
print('trimmed build vs the same reference:')
print('  invented a command                 : %d' % invented)
if skipped:
    print('  only in the reference, skipped     : %d' % skipped)
if failed:
    print('  could not be compared              : %d' % failed)
if size_ref:
    print()
    print('trimmed build: %d -> %d bytes (%.1f%% smaller)'
          % (size_ref, size_new, 100.0 * (size_ref - size_new) / size_ref))
sys.exit(1 if (lost or invented or failed) else 0)
PY
