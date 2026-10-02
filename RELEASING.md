# Releasing

Four repos, one dependency direction:

```
waldoctl (ABC/types)
   ├── parol6  (PAROL6 backend, PCrnjak/PAROL6-python-API)
   └── par6    (PAR6 backend,   Jepson2k/par6)
            └── waldo-commander (this repo)
```

Everything is installed from git URLs pinned in `pyproject.toml`. There is
no PyPI publish anywhere in the chain.

## Order

**waldoctl → (parol6, par6) → waldo-commander.** A backend cannot be
released against a waldoctl tag that does not exist yet, and WC cannot pin
a waldoctl its backends have not moved to.

Within a repo, **the pin bump and the tag are never the same commit**:

1. bump the pins you consume (they already exist), merge
2. verify (below)
3. tag

## Verify outside CI

**CI cannot verify a release, because CI is what hides the failure.** The
branch-matching in `.github/workflows/tests.yml` installs a dependency from
a same-named branch when one exists, so a pin that resolves to nothing —
or to a release missing a symbol the code imports — passes CI and fails for
everyone else.

That is not hypothetical. `par6` imported `StatusRate` from waldoctl while
WC pinned a waldoctl without it; every CI run was green because branch
matching quietly substituted a branch for the pin. It surfaced only when
the matching branch was pointed at `main`.

So, after each tag, in a throwaway venv, with no branch matching:

```bash
git ls-remote --tags --exit-code <url> <tag>     # the tag exists
python -m venv /tmp/rel && /tmp/rel/bin/pip install "<pkg> @ git+<url>@<tag>"
/tmp/rel/bin/python -c "import <pkg>"            # waldoctl and parol6 both
                                                 # call importlib.metadata.version()
                                                 # with no guard, so a bad
                                                 # install is an ImportError
```

And scan the tagged `pyproject.toml` for pins that no longer resolve —
split on the LAST `@`, since the URL contains one:

```bash
grep -oE 'git\+[^"]+' pyproject.toml | while read -r u; do
  u="${u%%#*}"                       # drop #subdirectory=... and friends
  ref="${u##*@}"; url="${u#git+}"; url="${url%@*}"
  if [ ${#ref} -eq 40 ] && [ -z "${ref//[0-9a-f]/}" ]; then
    echo "SHA  (unchecked) $url@$ref"; continue   # a bare SHA is not a ref
  fi
  git ls-remote --exit-code "$url" "$ref" >/dev/null 2>&1 \
    && echo "OK   $url@$ref" || echo "DEAD $url@$ref"
done
```

The two carve-outs are load-bearing: the par6 pin carries a
`#subdirectory=python` fragment that would otherwise be read as part of
the ref, and the vendored NiceGUI is pinned to a bare commit SHA, which
`ls-remote` cannot resolve at all. Without both, this reports two false
DEAD pins on a perfectly good file.

## par6 is the exception

`wheels.yml` has **no PyPI publish step**, and a `git+` URL never consumes
a release wheel. So `pip install ".[par6]"` is a compiler-and-conda
operation however the pin is written, and nothing in CI exercises the
wheel that the release actually ships.

Verify that leg by downloading the tag's x86_64 wheel from the GitHub
release and installing *that* into a clean venv.

Dry-run the release with `workflow_dispatch`, **not** an rc tag: the
workflow matches `refs/tags/v`, so `v0.3.0-rc1` would cut a real public
release. Dispatch runs all three build legs including the clean-venv smoke
test, and skips only the `release` job.

## What a waldoctl bump breaks downstream

Check these every time; they are the shape of breakage this chain produces.

- **A struct that mirrors a waldoctl wire tuple.** `ShapeBase.to_wire()`
  went from six elements to seven in 0.13 (`physics`). parol6's `ShapeWire`
  built itself by positional unpack, so every `set_shapes` raised
  `TypeError` until the field was added — defaulted, so both arities decode.
- **Anything re-deriving what waldoctl exports.** WC's shape editor listed
  the "common" shape fields itself instead of calling `param_names()`, so
  `physics` arrived looking like a dimension. waldoctl's docstring says to
  enumerate through it for exactly this reason.
- **Equality and hashing.** `RobotError` gained value equality and a hash in
  0.13. Code that deduplicates warnings must either compare raw wire tuples
  (version-proof) or ship with the bump — at the older pin those objects
  have identity equality, so every standing warning re-appends forever.

## Tags in flight

`waldoctl` v0.13.1 is current. par6 and parol6 have not yet been tagged
against it; WC still pins waldoctl v0.12.0 and par6 `@main`.
