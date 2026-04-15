# Copilot Instructions

## Linting

Lint and format all Python code with [ruff](https://docs.astral.sh/ruff/).

## Import Style

Import modules or submodules, then access objects through them. Do **not** import objects or functions directly from a submodule.

**Do:**

```python
from module import submodule

submodule.Object()
```

**Don't:**

```python
from module.submodule import MyObject

MyObject()
```

Use one import per line. Do **not** import multiple names in a single `from … import` statement.

**Do:**

```python
from module import submodule_a
from module import submodule_b
```

**Don't:**

```python
from module import submodule_a, submodule_b
```

## Type Checking
Please run mypy to check for type errors in the code. A few points about mypy:

We only support python 3.11 and above, so please do not use unnecessary imports
from the `typing` module, such as `List`, `Dict`, etc. Instead, please use the
built-in types, such as `list`, `dict`, etc, or the `collections.abc` module.
