"""Upstream licence identification: never guessed. No network."""

import pytest

from repogym.licenses import LicenseError, declared_spdx, identify, text_family

MIT = b"""The MIT License (MIT)
Copyright (c) 2015 Someone
Permission is hereby granted, free of charge, to any person obtaining a copy ..."""
BSD3 = b"""Copyright 2014 Pallets
Redistribution and use in source and binary forms, with or without modification, are
permitted provided that the following conditions are met:
1.  Redistributions of source code must retain the above copyright notice.
2.  Redistributions in binary form must reproduce the above copyright notice.
3.  Neither the name of the copyright holder nor the names of its contributors may be used."""
BSD2 = b"""Redistribution and use in source and binary forms, with or without modification:
1. Redistributions of source code must retain the above copyright notice.
2. Redistributions in binary form must reproduce the above copyright notice."""
LGPL = b"""GNU LESSER GENERAL PUBLIC LICENSE
Version 2.1, February 1999"""


def test_text_families():
    assert text_family(MIT.decode()) == "MIT"
    assert text_family(BSD3.decode()) == "BSD-3-Clause"
    assert text_family(BSD2.decode()) is None  # a BSD variant we don't recognise is not guessed
    assert text_family(LGPL.decode()) == "LGPL-2.1"
    assert text_family("All rights reserved.") is None


@pytest.mark.parametrize(
    ("pyproject", "spdx"),
    [
        ('license = "MIT"', "MIT"),
        ('license = "LGPL-2.1-OR-later"', "LGPL-2.1-or-later"),
        ('license = { text = "LGPL-2.1-or-later" }', "LGPL-2.1-or-later"),
        ('license = {file = "LICENSE.txt"}', None),
        ("", None),
    ],
)
def test_declared_spdx_is_canonicalised(pyproject, spdx):
    assert declared_spdx(f"[project]\n{pyproject}\n") == spdx


def test_declared_but_unknown_identifier_is_an_error():
    with pytest.raises(LicenseError, match="unrecognised"):
        declared_spdx('license = "WTFPL"')


def test_pyproject_declaration_is_cross_checked_with_the_text():
    found = identify({"LICENSE": MIT}, 'license = "MIT"')
    assert (found.spdx, found.source, found.file) == ("MIT", "pyproject", "LICENSE")
    with pytest.raises(LicenseError, match="reads as MIT"):
        identify({"LICENSE": MIT}, 'license = "BSD-3-Clause"')


def test_text_is_used_when_pyproject_only_points_at_the_file():
    found = identify({"LICENSE.txt": BSD3}, 'license = {file = "LICENSE.txt"}')
    assert (found.spdx, found.source) == ("BSD-3-Clause", "license text")


def test_lgpl_scope_is_never_guessed_from_text_alone():
    with pytest.raises(LicenseError, match="version scope"):
        identify({"LICENSE": LGPL}, None)
    assert identify({"LICENSE": LGPL}, 'license = "LGPL-2.1-or-later"').spdx == "LGPL-2.1-or-later"


def test_missing_ambiguous_or_unrecognised_files_are_errors():
    with pytest.raises(LicenseError, match="none"):
        identify({}, 'license = "MIT"')
    with pytest.raises(LicenseError, match="expected one"):
        identify({"LICENSE": MIT, "COPYING": LGPL}, None)
    with pytest.raises(LicenseError, match="not recognised"):
        identify({"LICENSE": BSD2}, None)
