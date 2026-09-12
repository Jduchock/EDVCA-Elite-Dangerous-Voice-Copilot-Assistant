#!/usr/bin/env python3
"""
Convert Nova's standalone kestrel-field-log.html into an artifact-ready page.

The Artifact tool wraps the file it is given in its own <!doctype>/<head>/<body>
skeleton, so the published file must NOT carry one of its own. This strips the
skeleton, keeps <title> + every <style> + the whole body, loads the two web
fonts Nova's CSS names but never fetches, adds responsive breakpoints, and
stamps the page with the time it was mirrored.

Usage:  python3 publish_prep.py <input.html> <output.html>
"""
import sys, re, io, datetime

FONTS = ('<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=Chakra+Petch:wght@400;500;600&'
         'family=IBM+Plex+Mono:wght@400;500&display=swap">')

# Nova writes for a desktop browser; these only take effect on small screens.
EXTRA_CSS = """
  /* --- added at publish time; does not exist in the local copy --- */
  .tblwrap{overflow-x:auto}
  table{min-width:0}
  @media (max-width:900px){
    .row.r5{grid-template-columns:repeat(2,1fr)}
    .row.r2,.row.r3,.cols,.exogrid,.rankgrid{grid-template-columns:1fr}
    .tl{grid-template-columns:repeat(2,1fr)}
    .cls{grid-template-columns:repeat(2,1fr)}
    body{padding:14px 14px 24px}
    h1{font-size:20px}
    header{flex-direction:column;align-items:flex-start;gap:10px}
    .stamp{text-align:left}
  }
  @media (max-width:560px){
    .row.r5,.cls,.tl,.gnums{grid-template-columns:1fr}
    table{font-size:10.5px}
  }
  @media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
  th.s:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
  .syncstamp{margin-top:10px;padding-top:9px;border-top:1px solid var(--line);
    font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:#55524b}
"""

def fingerprint(raw):
    """Pull the few numbers that identify WHICH build of the page this is.

    They go in the sync stamp so the published copy can be compared against
    the local one at a glance - distance flown is the field that moves most,
    so it is the quickest tell that the two are out of step.
    Every field is optional: if Nova restyles the page and a pattern stops
    matching, that line just gets shorter. It must never break the publish.
    """
    out = {}
    try:
        m = re.search(r'<b>DAY\s+(\d+)</b>', raw, re.I)
        if m:
            out['day'] = m.group(1)

        m = re.search(r'Distance Flown</div>\s*<div class="v">\s*([\d,]+)',
                      raw, re.I)
        if m:
            out['distance'] = m.group(1)

        # "108 samples, 47 species" in the ladder note is the authoritative pair
        m = re.search(r'([\d,]+)\s+samples,\s*([\d,]+)\s+species', raw, re.I)
        if m:
            out['samples'], out['species'] = m.group(1), m.group(2)
        else:
            m = re.search(r'([\d,]+)\s+samples', raw, re.I)
            if m:
                out['samples'] = m.group(1)
    except Exception:
        pass
    return out


def convert(raw, when=None):
    when = when or datetime.datetime.now(datetime.timezone.utc)

    # --- head: everything between <head> and </head>, minus meta/doctype noise
    m = re.search(r'<head[^>]*>(.*?)</head>', raw, re.S | re.I)
    if not m:
        raise SystemExit('no <head> found - is this the standalone field log?')
    head = m.group(1)
    head = re.sub(r'<meta[^>]*>', '', head, flags=re.I).strip()

    # --- body: everything between <body...> and </body>
    m = re.search(r'<body[^>]*>(.*?)</body>', raw, re.S | re.I)
    if not m:
        raise SystemExit('no <body> found - is this the standalone field log?')
    body = m.group(1).strip()

    # fonts go straight after the title so they start loading first
    if '</title>' in head:
        head = head.replace('</title>', '</title>\n' + FONTS, 1)
    else:
        head = FONTS + '\n' + head

    # publish-time CSS rides at the end of the first <style> block
    head = head.replace('</style>', EXTRA_CSS + '</style>', 1)

    # wide tables get their own scroll container rather than pushing the page
    body = re.sub(r'(<table\b)', r'<div class="tblwrap">\1', body)
    body = re.sub(r'(</table>)', r'\1</div>', body)

    stamp = when.strftime('%d %b %Y  %H:%M UTC').upper()
    fp = fingerprint(raw)
    parts = ['Published copy mirrored from Nova&rsquo;s live field log', stamp]
    if fp.get('day'):
        parts.append('Day %s' % fp['day'])
    if fp.get('distance'):
        parts.append('%s ly flown' % fp['distance'])
    if fp.get('samples'):
        bit = '%s samples' % fp['samples']
        if fp.get('species'):
            bit += ', %s species' % fp['species']
        parts.append(bit)
    sync = ('\n<div class="syncstamp">%s</div>\n'
            % ' &nbsp;&middot;&nbsp; '.join(parts))

    return head + '\n' + body + sync

if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    src = io.open(sys.argv[1], encoding='utf-8', errors='replace').read()
    out = convert(src)
    io.open(sys.argv[2], 'w', encoding='utf-8').write(out)
    print('wrote %s  (%d bytes)' % (sys.argv[2], len(out)))
