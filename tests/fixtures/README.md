# Test fixtures

- `text_page.pdf` — a blank page, then page 2 of the v2.x user manual (index 1) — its
  contents page: real, extractable text in many short runs, which the Pointer /
  edit-text / row-area tests need. It used to be read straight from
  `docs/PatchworkPDF_Manual.pdf`, but that file is the SHIPPED manual and is
  rewritten whenever the manual is updated — which silently changed what those
  tests exercised. Keep this copy frozen.
