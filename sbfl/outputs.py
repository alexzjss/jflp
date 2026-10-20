"""Leitura das saídas do Jaguar (formato do Jaguar 1: XML com <requirements>)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass
class Req:
    cls: str
    line: int
    sus: float
    ef: int
    ep: int
    nf: int
    np: int


def outer(cls: str) -> str:
    """Classe externa de uma classe interna (a.B$Inner -> a.B)."""
    return cls.split("$", 1)[0]


def parse_xml_jaguar1(path) -> list[Req]:
    reqs = []
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag == "requirements":
            a = el.attrib
            reqs.append(Req(a.get("name", ""), int(a.get("location", 0)),
                            float(a.get("suspicious-value", 0.0)),
                            int(a.get("cef", 0)), int(a.get("cep", 0)),
                            int(a.get("cnf", 0)), int(a.get("cnp", 0))))
            el.clear()
    return reqs
