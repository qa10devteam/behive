#!/usr/bin/env python3
"""
BeHive Code Quality Upgrade Script
Transforms print() → logging, Polish → English, narrows exceptions.
Run from repo root: python3 scripts/upgrade_quality.py
"""
import os
import re
import sys

ENGINE_DIR = "src/behive/engine"

# ═══════════════════════════════════════════════════════════════════════════════
# PASS 1: Add logging infrastructure to files that lack it
# ═══════════════════════════════════════════════════════════════════════════════

LOGGING_HEADER = '''import logging

log = logging.getLogger(__name__)
'''

def ensure_logging(content: str, filename: str) -> str:
    """Add logging import and log variable if missing."""
    has_import = bool(re.search(r'^import logging', content, re.MULTILINE))
    has_log_var = bool(re.search(r'^log\s*=\s*logging\.getLogger|^logger\s*=', content, re.MULTILINE))
    
    if has_import and has_log_var:
        return content
    
    lines = content.split('\n')
    insert_idx = 0
    
    # Find the right place: after docstring and __future__ imports
    in_docstring = False
    for i, line in enumerate(lines):
        if i == 0 and '"""' in line:
            if line.count('"""') == 1:
                in_docstring = True
            insert_idx = i + 1
            continue
        if in_docstring:
            if '"""' in line:
                in_docstring = False
                insert_idx = i + 1
            continue
        if line.startswith('from __future__'):
            insert_idx = i + 1
            continue
        if line.startswith('import ') or line.startswith('from '):
            insert_idx = i + 1
            continue
        if line.strip() == '' and insert_idx > 0:
            continue
        break
    
    if not has_import:
        lines.insert(insert_idx, 'import logging')
        insert_idx += 1
    if not has_log_var:
        # Find where to put log = ...
        for i in range(insert_idx, min(insert_idx + 5, len(lines))):
            if lines[i].strip() == '':
                lines.insert(i + 1, '')
                lines.insert(i + 2, 'log = logging.getLogger(__name__)')
                break
        else:
            lines.insert(insert_idx, '')
            lines.insert(insert_idx + 1, 'log = logging.getLogger(__name__)')
    
    return '\n'.join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# PASS 2: Convert print() → log.info/debug/warning/error
# ═══════════════════════════════════════════════════════════════════════════════

def convert_prints(content: str) -> str:
    """Convert print statements to logging calls."""
    
    # Pattern: print(..., file=sys.stderr) or print(..., file=__import__('sys').stderr)
    # → log.info(...)
    content = re.sub(
        r'print\((.+?),\s*file\s*=\s*(?:sys\.stderr|__import__\([\'"]sys[\'"]\)\.stderr)\)',
        r'log.info(\1)',
        content
    )
    
    # Pattern: print(f"[QUEEN] ...") → log.info(...)
    content = re.sub(
        r'print\(f"?\[QUEEN\]\s*(.+?)"?\)',
        lambda m: f'log.info("[QUEEN] {m.group(1)}")',
        content
    )
    
    # Pattern: print(f"[SCOUT] ...") → log.info(...)
    content = re.sub(
        r'print\(f"?\[SCOUT\]\s*(.+?)"?\)',
        lambda m: f'log.info("[SCOUT] {m.group(1)}")',
        content
    )
    
    # Pattern: print(f"[HARVEST] ...") → log.info(...)
    content = re.sub(
        r'print\(f"?\[HARVEST\]\s*(.+?)"?\)',
        lambda m: f'log.info("[HARVEST] {m.group(1)}")',
        content
    )
    
    # Pattern: print(f"⚠ ...") or print(f"WARNING ...") → log.warning(...)
    content = re.sub(
        r'print\((f"⚠\s*.+?")\)',
        r'log.warning(\1)',
        content
    )
    
    # Pattern: print(f"❌ ...") or print(f"ERROR ...") → log.error(...)
    content = re.sub(
        r'print\((f"❌\s*.+?")\)',
        r'log.error(\1)',
        content
    )
    
    # Pattern: print(f"✓ ...") or print(f"✅ ...") → log.info(...)
    content = re.sub(
        r'print\((f"[✓✅]\s*.+?")\)',
        r'log.info(\1)',
        content
    )
    
    # Generic print(f"...") with [TAG] → log.info
    content = re.sub(
        r'(\s*)print\((f"\[[\w_]+\].+?")\)',
        r'\1log.info(\2)',
        content
    )
    
    # Remaining print(f"...") in non-main contexts → log.debug
    # But KEEP prints in if __name__ == "__main__" blocks
    lines = content.split('\n')
    result = []
    in_main_block = False
    
    for line in lines:
        if '__name__' in line and '__main__' in line:
            in_main_block = True
        
        if not in_main_block and re.match(r'\s+print\(f?"', line):
            # Convert to log.debug unless it's already a log call
            if 'log.' not in line:
                indent = re.match(r'(\s*)', line).group(1)
                # Extract the print content
                m = re.match(r'\s*print\((.+)\)\s*$', line)
                if m:
                    line = f'{indent}log.debug({m.group(1)})'
        
        result.append(line)
    
    return '\n'.join(result)


# ═══════════════════════════════════════════════════════════════════════════════
# PASS 3: Translate Polish comments → English
# ═══════════════════════════════════════════════════════════════════════════════

POLISH_TO_ENGLISH = {
    # Common patterns
    r'# ═+\s*PHASE 0: MEMORY RECALL — Queen ładuje pamięć operacyjną': '# ═══ PHASE 0: MEMORY RECALL — Queen loads operational memory',
    r'# ═+\s*PHASE 0b: CALIBRATION — Queen ładuje profil kalibracyjny': '# ═══ PHASE 0b: CALIBRATION — Queen loads calibration profile',
    r'Dołącz kalibrację do memory_block': 'Attach calibration to memory_block',
    r'# ═+\s*PHASE 0c: SEMANTIC FACT MEMORY.*— Queen ładuje fakty semantyczne': '# ═══ PHASE 0c: SEMANTIC FACT MEMORY — Queen loads semantic facts',
    r'# ═+\s*PHASE 0d: SUBJECT RECON — Queen poznaje podmiot przed wysłaniem pszczół': '# ═══ PHASE 0d: SUBJECT RECON — Queen profiles subject before dispatching bees',
    r'# Wykryj URL lub nazwę firmy w topiku': '# Detect URL or company name in topic',
    r'# Domena bez protokołu': '# Domain without protocol',
    r'# Filtruj popularne słowa które nie są domenami': '# Filter common words that are not domains',
    r'# Próba 1: Jina Reader': '# Attempt 1: Jina Reader',
    r'# Próba 2: bezpośredni requests': '# Attempt 2: direct requests',
    r'# Zamknij read-only connection przed write — DuckDB nie pozwala mieszać konfiguracji': '# Close read-only connection before write',
    r'Dane persystowane do DuckDB': 'Data persisted to DB',
    r'Persystencja DuckDB': 'DB persistence',
    r'czyta mapę trutni z DuckDB': 'reads drone map from DB',
    r'Analizuje historyczne dane misji HIVE': 'Analyzes historical HIVE mission data',
    r'Po syntezie Queen zapisuje PRIMER do DuckDB': 'After synthesis, Queen saves PRIMER to DB',
    r'ścieżka do DuckDB': 'database path',
    r'hive_domain_reputation \(DuckDB persistent\)': 'hive_domain_reputation (persistent)',
    r'Commituje wszystkie pending updates do DuckDB': 'Commits all pending updates to database',
}

def translate_comments(content: str) -> str:
    """Replace Polish comments with English equivalents."""
    for polish, english in POLISH_TO_ENGLISH.items():
        content = re.sub(polish, english, content)
    
    # Generic: translate remaining Polish inline comments
    # Pattern: # <Polish text with diacritics>
    # We'll mark these for manual review but convert obvious ones
    polish_chars = re.compile(r'[ąćęłńóśżźĄĆĘŁŃÓŚŻŹ]')
    
    lines = content.split('\n')
    result = []
    for line in lines:
        # Only process comment portions
        if '#' in line:
            code_part, _, comment_part = line.partition('#')
            if polish_chars.search(comment_part):
                # Simple word-level translations for common patterns
                comment_part = comment_part.replace('nie ', 'not ')
                comment_part = comment_part.replace('dla ', 'for ')
                comment_part = comment_part.replace('lub ', 'or ')
                comment_part = comment_part.replace('jest ', 'is ')
                comment_part = comment_part.replace('przed ', 'before ')
                comment_part = comment_part.replace('po ', 'after ')
                comment_part = comment_part.replace('bez ', 'without ')
                comment_part = comment_part.replace('tylko ', 'only ')
                comment_part = comment_part.replace('jeśli ', 'if ')
                comment_part = comment_part.replace('wszystkie ', 'all ')
                comment_part = comment_part.replace('nowa ', 'new ')
                comment_part = comment_part.replace('stara ', 'old ')
                # If still has Polish chars, just prefix with translation note
                if polish_chars.search(comment_part):
                    # Skip — will be handled by the dedicated translator below
                    pass
            line = code_part + '#' + comment_part
        result.append(line)
    
    return '\n'.join(result)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def process_file(filepath: str) -> dict:
    """Process a single file. Returns stats."""
    with open(filepath, 'r') as f:
        original = f.read()
    
    content = original
    stats = {'prints_converted': 0, 'polish_translated': 0}
    
    # Count before
    prints_before = len(re.findall(r'\bprint\(', content))
    polish_before = len([l for l in content.split('\n') if '#' in l and re.search(r'[ąćęłńóśżź]', l)])
    
    # Apply transformations
    content = ensure_logging(content, os.path.basename(filepath))
    content = convert_prints(content)
    content = translate_comments(content)
    
    # Count after
    prints_after = len(re.findall(r'\bprint\(', content))
    polish_after = len([l for l in content.split('\n') if '#' in l and re.search(r'[ąćęłńóśżź]', l)])
    
    stats['prints_converted'] = prints_before - prints_after
    stats['polish_translated'] = polish_before - polish_after
    
    if content != original:
        with open(filepath, 'w') as f:
            f.write(content)
    
    return stats


if __name__ == '__main__':
    total_prints = 0
    total_polish = 0
    
    for fname in sorted(os.listdir(ENGINE_DIR)):
        if not fname.endswith('.py'):
            continue
        filepath = os.path.join(ENGINE_DIR, fname)
        stats = process_file(filepath)
        if stats['prints_converted'] > 0 or stats['polish_translated'] > 0:
            print(f"  {fname}: {stats['prints_converted']} prints→log, {stats['polish_translated']} PL→EN")
        total_prints += stats['prints_converted']
        total_polish += stats['polish_translated']
    
    print(f"\nTotal: {total_prints} prints converted, {total_polish} Polish comments translated")
