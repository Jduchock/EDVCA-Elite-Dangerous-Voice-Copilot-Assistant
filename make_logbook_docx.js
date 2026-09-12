/*
 * Nova's Field Logbook  ->  Microsoft Word (.docx)
 * =================================================
 * Turns reference/nova-logbook.html into the Word document CMDR Mister John
 * pastes into Inara. Word's clipboard hands a crude HTML editor clean bold and
 * italic without the wrapper divs and inline styles a browser drags along,
 * which is why this exists rather than copying from the rendered page.
 *
 * THIS IS THE CANONICAL FORMAT. Keep it. Do not redesign it.
 *
 * HOW TO RUN IT
 *   The `docx` npm package is preinstalled in Claude's cloud workspace but is
 *   NOT on this PC, so the script runs there, not here. In a Claude session:
 *     1. stage reference/nova-logbook.html and this file into the workspace
 *     2. put them side by side, with the logbook named logbook-src.html
 *     3. node make_logbook_docx.js
 *     4. commit "Nova - Field Logbook.docx" back to C:\Users\johnd\Downloads\
 *
 * WORKFLOW
 *   end of watch  ->  add the day to reference/nova-logbook.html
 *                 ->  regenerate this .docx
 *                 ->  open in Word, select the newest day, copy, paste to Inara
 *
 * Layout: US Letter, Calibri, 0.75" margins. Each day is a real
 * HeadingLevel.HEADING_2 so Word's navigation pane lists them. Inline <b> and
 * <i> from the source become bold and italic runs; everything else is stripped.
 */
const fs = require('fs');
const { Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
        BorderStyle } = require('docx');

const SRC = process.argv[2] || 'logbook-src.html';
const OUT = process.argv[3] || 'Nova - Field Logbook.docx';
const src = fs.readFileSync(SRC, 'utf8');

const body = src.split('<hr>')[1];
const blocks = body.split(/(?=<h2>)/).filter(b => b.trim().startsWith('<h2>'));
const lastHr = body.lastIndexOf('<hr>');
blocks[blocks.length - 1] = blocks[blocks.length - 1].split('<hr>')[0];
const closing = body.slice(lastHr).match(/<p>([\s\S]*?)<\/p>/g) || [];

const ent = s => s
  .replace(/&mdash;/g, '\u2014').replace(/&ndash;/g, '\u2013')
  .replace(/&rsquo;/g, '\u2019').replace(/&nbsp;/g, ' ')
  .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&#8322;/g, '\u2082');

// inline <b>/<i> -> TextRuns, so emphasis survives the paste
function runs(html, base = {}) {
  const out = [];
  const re = /<(b|i|strong|em)>([\s\S]*?)<\/\1>/g;
  let last = 0, m;
  const push = (t, o) => { t = ent(t.replace(/<[^>]+>/g, '')).replace(/\s+/g, ' ');
                           if (t) out.push(new TextRun({ text: t, ...base, ...o })); };
  while ((m = re.exec(html)) !== null) {
    push(html.slice(last, m.index), {});
    push(m[2], (m[1] === 'b' || m[1] === 'strong') ? { bold: true } : { italics: true });
    last = re.lastIndex;
  }
  push(html.slice(last), {});
  return out;
}

const children = [];
const standfirst = (src.match(/<p style="margin-top:0;"><i>([\s\S]*?)<\/i><\/p>/) || [])[1]
  || 'Kept by <b>NOVA</b>, robotic assistant aboard the fleet carrier <b>HOME</b>.';

children.push(new Paragraph({
  children: [new TextRun({ text: "Nova's Field Logbook", bold: true, size: 44 })],
  spacing: { after: 80 },
}));
children.push(new Paragraph({
  children: runs(standfirst, { italics: true, size: 20 }),
  spacing: { after: 200 },
  border: { bottom: { style: BorderStyle.SINGLE, size: 6, space: 8, color: '999999' } },
}));

for (const b of blocks) {
  const head = ent(b.match(/<h2>([\s\S]*?)<\/h2>/)[1].replace(/\s+/g, ' ').trim());
  children.push(new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 320, after: 100 },
    children: [new TextRun({ text: head, bold: true, size: 30 })],
  }));
  for (const p of (b.match(/<p>[\s\S]*?<\/p>/g) || [])) {
    children.push(new Paragraph({
      children: runs(p.replace(/^<p>/, '').replace(/<\/p>$/, '')),
      spacing: { after: 140 }, alignment: AlignmentType.LEFT }));
  }
}

children.push(new Paragraph({ text: '', spacing: { before: 200 },
  border: { top: { style: BorderStyle.SINGLE, size: 6, space: 8, color: '999999' } } }));
for (const p of closing) {
  children.push(new Paragraph({
    children: runs(p.replace(/^<p>/, '').replace(/<\/p>$/, '')), spacing: { after: 140 } }));
}
children.push(new Paragraph({
  children: [new TextRun({ text: '\u2014 NOVA, aboard HOME. Log continues.',
                           italics: true, size: 18, color: '666666' })],
  spacing: { before: 200 } }));

const doc = new Document({
  styles: { default: { document: { run: { font: 'Calibri', size: 22 },
                                   paragraph: { spacing: { line: 300 } } } } },
  sections: [{
    properties: { page: { size: { width: 12240, height: 15840 },
                          margin: { top: 1080, bottom: 1080, left: 1080, right: 1080 } } },
    children,
  }],
});

Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync(OUT, buf);
  console.log('wrote ' + OUT + '  (' + buf.length + ' bytes, ' + blocks.length + ' day entries)');
});
