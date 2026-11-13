/*
  Minimal RFC-4180 CSV reader, shared by the PhoneBook contact import and the
  personalised broadcast import. Pure function, no DOM, no network.

  Handles quoted fields, escaped quotes (""), CRLF, and a UTF-8 BOM. Blank lines
  are dropped so a trailing newline does not become an empty row that then gets
  reported as a validation error.

  Returns an array of arrays — the caller decides which row is the header.
*/
window.parseCSV = function (text) {
  text = text.replace(/^﻿/, '');          // strip BOM
  var out = [], row = [], val = '', inQ = false, i = 0;
  while (i < text.length) {
    var c = text[i];
    if (inQ) {
      if (c === '"') {
        if (text[i + 1] === '"') { val += '"'; i += 2; continue; }
        inQ = false; i++; continue;
      }
      val += c; i++; continue;
    }
    if (c === '"') { inQ = true; i++; continue; }
    if (c === ',') { row.push(val); val = ''; i++; continue; }
    if (c === '\r') { i++; continue; }
    if (c === '\n') { row.push(val); out.push(row); row = []; val = ''; i++; continue; }
    val += c; i++;
  }
  if (val !== '' || row.length) { row.push(val); out.push(row); }
  return out.filter(function (r) { return r.some(function (v) { return v.trim() !== ''; }); });
};

/*
  readSheetFile(file) → Promise<string[][]>   (row 0 = the headings)

  The one way the import dialogs read a chosen file. A .csv is parsed here as before; an
  Excel file (.xlsx) is sent to the server, which reads it (first sheet, values only)
  and returns the same grid. Excel stores a long number exactly, so a WhatsApp number
  read this way keeps all its digits.
*/
window.isSheetFile = function (file) {
  var n = (file && file.name || '').toLowerCase();
  return n.endsWith('.xlsx') || n.endsWith('.xlsm') ||
    (file && file.type === 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
};

window.readSheetFile = function (file) {
  if (!window.isSheetFile(file)) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () { resolve(window.parseCSV(String(reader.result))); };
      reader.onerror = function () { reject(new Error('Could not read that file.')); };
      reader.readAsText(file);
    });
  }
  var fd = new FormData();
  fd.append('file', file, file.name);
  return fetchWithAuth('/api/phonebooks/import/parse', { method: 'POST', body: fd })
    .then(async function (res) {
      var body = await res.json().catch(function () { return {}; });
      if (!res.ok) throw new Error(body.detail || 'Could not read that Excel file.');
      return [body.headers].concat(body.rows || []);
    });
};

/* Download a headings-only sample from the server in either format. */
window.downloadSample = async function (format, columns, name) {
  var q = 'format=' + encodeURIComponent(format) +
          (columns ? '&columns=' + encodeURIComponent(columns.join(',')) : '') +
          (name ? '&name=' + encodeURIComponent(name) : '');
  var res = await fetchWithAuth('/api/phonebooks/import/sample?' + q);
  if (!res.ok) { if (window.toast) toast('Could not download the sample.', 'error'); return; }
  var blob = await res.blob();
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = (name || 'phonebook_sample') + '.' + format;
  a.click();
  setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
};
