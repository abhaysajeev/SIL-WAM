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
