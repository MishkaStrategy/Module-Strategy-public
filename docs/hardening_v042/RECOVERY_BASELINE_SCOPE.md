# Recovery baseline scope

The first exact-head recovery gate must establish the executable baseline without modifying behavior.

Required evidence:

- Python 3.12 and 3.13;
- isolated installation and `pip check`;
- compileall;
- Ruff lint and format check;
- strict MyPy for `src/modular_core`;
- root tests with branch coverage at or above 85%;
- Session Range, Market Regime and FVG compatibility suites;
- wheel and sdist build;
- Twine metadata validation;
- clean wheel-only installation and public import smoke;
- confirmation that transport-only files are absent.

Any baseline failure is treated as a real recovery finding and must be fixed before completeness review 4.
