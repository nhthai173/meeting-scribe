Re-export an existing MoM markdown file to HTML and PDF.

The input is: $ARGUMENTS
(markdown path, optionally followed by `--template <name>` — `editorial` or `vn`)

Run:
```
python <project_root>/mom_export.py "<file.md>" [--template <name>]
```

`<project_root>` is the directory containing `mom_export.py`.
Without `--template`, the exporter reuses the template recorded in the `.md`
(`<!-- mom-template: ... -->`, written on the last export that passed one), else `editorial`.

Then print the paths of the generated `.html` and `.pdf` files.
