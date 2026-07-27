# Repository Agent Instructions

## Validation Policy

GitHub Actions is the authoritative full validation gate for pull requests. Do
not run the complete local test suite by default.

Before pushing, run only checks proportional to the changed surface:

- Python behavior: the directly affected pytest files or test cases, plus Ruff
  on changed Python files.
- Shared Python contracts or typing: add targeted Pyright only when the change
  can affect typed interfaces outside the edited module.
- Web behavior: `npm run check --prefix web`; run `npm run build --prefix web`
  when committed `web_static` assets must change.
- Packaging: leave cross-platform build and distribution smoke tests to GitHub
  Actions unless packaging code or release metadata changed.

Run the full local suite only when the user explicitly requests it, GitHub CI is
unavailable, or a failure cannot be reproduced with a focused command. A direct
deployment before CI completes still requires focused tests for every changed
runtime path; it does not require duplicating the entire CI matrix locally.

After focused checks pass, push the branch, open or update the pull request, and
use the required GitHub checks as the final merge signal.
