import re
from pathlib import Path
p = Path('文字冒险游戏_fixed8.py')
text = p.read_text(encoding='utf-8')
lines = text.splitlines()

global_names = set()
for i, line in enumerate(lines, 1):
    m = re.match(r"\s*global\s+(.*)", line)
    if m:
        names = [x.strip() for x in m.group(1).split(',') if x.strip()]
        for n in names:
            # remove any inline comments
            n = n.split('#',1)[0].strip()
            if n:
                global_names.add(n)

assignments = {}
for name in sorted(global_names):
    pat = re.compile(rf"(^|\W){re.escape(name)}\s*=", re.M)
    found = bool(pat.search(text))
    assignments[name] = found

print('Found', len(global_names), 'global symbols.\n')
missing = [n for n, f in assignments.items() if not f]
if missing:
    print('Potentially missing top-level assignments for:')
    for n in missing:
        print('-', n)
else:
    print('All globals have at least one assignment occurrence in file text.')

# Also list globals that are assigned only inside functions (heuristic): check assignments line numbers
for name in sorted(global_names):
    lines_with = [i+1 for i,l in enumerate(lines) if re.search(rf"(^|\W){re.escape(name)}\s*=", l)]
    if lines_with:
        print(f"\n{name}: first assignment at line {lines_with[0]} (sample lines: {lines_with[:3]})")
    else:
        print(f"\n{name}: no assignment found")
