# Publishing mayi

Do these in order. Use TestPyPI first so a mistake costs nothing: a version number on the real PyPI can never be reused.

## 1. GitHub

    cd mayi-code
    git init && git add . && git commit -m "mayi 0.3.0"
    # create an empty PUBLIC repo named "mayi" at https://github.com/new (no README, no licence)
    git branch -M main
    git remote add origin https://github.com/<your-username>/mayi.git
    git push -u origin main

Then add your repo URL to `pyproject.toml`:

    [project.urls]
    Homepage = "https://github.com/<your-username>/mayi"

## 2. PyPI

1. Make accounts at https://test.pypi.org and https://pypi.org. Turn on 2FA. Create an API token in each (Account settings > API tokens).
2. Build and check:

       pip install build twine
       rm -rf dist && python -m build && twine check dist/*

3. Practice upload (username `__token__`, password = the TestPyPI token):

       twine upload --repository testpypi dist/*
       python -m venv /tmp/t && /tmp/t/bin/pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple mayi

4. Real upload (username `__token__`, password = the PyPI token):

       twine upload dist/*

After that, `pip install mayi` works for everyone. For the next release, raise `version` in `pyproject.toml` and repeat step 2.

## Before you click publish

- The name `mayi` was free on PyPI when this was prepared. The first upload claims it.
- The repository and the package become public. Anyone can read and copy the code (Apache-2.0 licence).
- Never commit `.mayi-keys`, `.env` or an API key. `.gitignore` already excludes them.
