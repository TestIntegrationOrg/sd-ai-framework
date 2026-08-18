from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Mapping, Sequence

from sdai.contracts import (
    CompatibilityDirection,
    ContractFinding,
    ContractProvenance,
    ContractSeverity,
    ContractSnapshot,
)


_MAX_TOKENS = 250_000
_MAX_NESTING = 64
_MAX_FIELD_NUMBER = 536_870_911
_RESERVED_FIELD_MIN = 19_000
_RESERVED_FIELD_MAX = 19_999
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IMPORT_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_SCALARS = frozenset(
    {
        "double",
        "float",
        "int32",
        "int64",
        "uint32",
        "uint64",
        "sint32",
        "sint64",
        "fixed32",
        "fixed64",
        "sfixed32",
        "sfixed64",
        "bool",
        "string",
        "bytes",
    }
)


class _ProtoParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    value: str
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class ProtoImport:
    path: str
    modifier: str


@dataclass(frozen=True, slots=True)
class ProtoField:
    name: str
    number: int
    type_name: str
    cardinality: str


@dataclass(frozen=True, slots=True)
class ProtoMessage:
    full_name: str
    fields: tuple[ProtoField, ...]
    reserved_ranges: tuple[tuple[int, int], ...]
    reserved_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProtoEnumValue:
    name: str
    number: int


@dataclass(frozen=True, slots=True)
class ProtoEnum:
    full_name: str
    values: tuple[ProtoEnumValue, ...]
    reserved_ranges: tuple[tuple[int, int], ...]
    reserved_names: tuple[str, ...]
    allow_alias: bool


@dataclass(frozen=True, slots=True)
class ProtoRpc:
    name: str
    request_type: str
    response_type: str
    client_streaming: bool
    server_streaming: bool


@dataclass(frozen=True, slots=True)
class ProtoService:
    full_name: str
    rpcs: tuple[ProtoRpc, ...]


@dataclass(frozen=True, slots=True)
class ProtoDocument:
    syntax: str
    package: str
    imports: tuple[ProtoImport, ...]
    messages: tuple[ProtoMessage, ...]
    enums: tuple[ProtoEnum, ...]
    services: tuple[ProtoService, ...]


@dataclass(frozen=True, slots=True)
class _Parsed:
    document: ProtoDocument | None
    findings: tuple[ContractFinding, ...]


def _provenance(snapshot: ContractSnapshot, pointer: str | None = None) -> ContractProvenance:
    return ContractProvenance(
        source_id=snapshot.source.source_id,
        source_path=snapshot.source.path,
        source_sha256=snapshot.sha256,
        pointer=pointer,
    )


def _finding(
    snapshot: ContractSnapshot,
    code: str,
    message: str,
    *,
    pointer: str | None = None,
    compatibility: CompatibilityDirection = CompatibilityDirection.NONE,
    severity: ContractSeverity = ContractSeverity.ERROR,
) -> ContractFinding:
    return ContractFinding(
        code=code,
        severity=severity,
        message=message,
        compatibility=compatibility,
        provenance=_provenance(snapshot, pointer),
    )


def _symbol_pointer(kind: str, name: str, suffix: str = "") -> str:
    escaped = name.replace("~", "~0").replace("/", "~1")
    return f"/{kind}/{escaped}{suffix}"


def _tokenize(text: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    index = 0
    line = 1
    column = 1
    length = len(text)

    def advance(char: str) -> None:
        nonlocal line, column
        if char == "\n":
            line += 1
            column = 1
        else:
            column += 1

    while index < length:
        char = text[index]
        if char.isspace():
            advance(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            while index < length and text[index] != "\n":
                advance(text[index])
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            start_line, start_column = line, column
            advance("/")
            advance("*")
            index += 2
            closed = False
            while index < length:
                if text[index] == "*" and index + 1 < length and text[index + 1] == "/":
                    advance("*")
                    advance("/")
                    index += 2
                    closed = True
                    break
                advance(text[index])
                index += 1
            if not closed:
                raise _ProtoParseError(
                    f"unterminated block comment at line {start_line}, column {start_column}"
                )
            continue

        start_line, start_column = line, column
        if char in {'"', "'"}:
            quote = char
            index += 1
            advance(char)
            value: list[str] = []
            while index < length:
                current = text[index]
                if current == quote:
                    index += 1
                    advance(current)
                    break
                if current == "\n":
                    raise _ProtoParseError(
                        f"unterminated string at line {start_line}, column {start_column}"
                    )
                if current == "\\":
                    if index + 1 >= length:
                        raise _ProtoParseError(
                            f"unterminated string escape at line {start_line}, column {start_column}"
                        )
                    escaped = text[index + 1]
                    escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'"}
                    if escaped not in escapes:
                        raise _ProtoParseError(
                            f"unsupported string escape '\\{escaped}' at line {line}, column {column}"
                        )
                    value.append(escapes[escaped])
                    advance(current)
                    advance(escaped)
                    index += 2
                    continue
                value.append(current)
                advance(current)
                index += 1
            else:
                raise _ProtoParseError(
                    f"unterminated string at line {start_line}, column {start_column}"
                )
            tokens.append(_Token("string", "".join(value), start_line, start_column))
        elif char.isalpha() or char == "_":
            end = index + 1
            while end < length and (text[end].isalnum() or text[end] == "_"):
                end += 1
            value = text[index:end]
            tokens.append(_Token("ident", value, start_line, start_column))
            while index < end:
                advance(text[index])
                index += 1
        elif char.isdigit():
            end = index + 1
            while end < length and text[end].isdigit():
                end += 1
            value = text[index:end]
            tokens.append(_Token("number", value, start_line, start_column))
            while index < end:
                advance(text[index])
                index += 1
        elif char in "{}[]();=,<>.-+":
            tokens.append(_Token("symbol", char, start_line, start_column))
            advance(char)
            index += 1
        else:
            raise _ProtoParseError(
                f"unsupported token {char!r} at line {start_line}, column {start_column}"
            )

        if len(tokens) > _MAX_TOKENS:
            raise _ProtoParseError(f"protobuf token count exceeds {_MAX_TOKENS}")

    tokens.append(_Token("eof", "", line, column))
    return tuple(tokens)


class _Parser:
    def __init__(self, tokens: Sequence[_Token]) -> None:
        self.tokens = tokens
        self.index = 0
        self.syntax = "proto2"
        self.package = ""
        self.imports: list[ProtoImport] = []
        self.messages: list[ProtoMessage] = []
        self.enums: list[ProtoEnum] = []
        self.services: list[ProtoService] = []
        self._depth = 0

    @property
    def current(self) -> _Token:
        return self.tokens[self.index]

    def _take(self) -> _Token:
        token = self.current
        self.index += 1
        return token

    def _accept(self, value: str) -> bool:
        if self.current.value == value:
            self.index += 1
            return True
        return False

    def _expect(self, value: str) -> _Token:
        token = self.current
        if token.value != value:
            self._fail(f"expected {value!r}, found {token.value!r}", token)
        self.index += 1
        return token

    def _expect_kind(self, kind: str, label: str) -> _Token:
        token = self.current
        if token.kind != kind:
            self._fail(f"expected {label}, found {token.value!r}", token)
        self.index += 1
        return token

    def _fail(self, message: str, token: _Token | None = None) -> None:
        point = token or self.current
        raise _ProtoParseError(f"{message} at line {point.line}, column {point.column}")

    def _enter(self) -> None:
        self._depth += 1
        if self._depth > _MAX_NESTING:
            self._fail(f"protobuf nesting exceeds {_MAX_NESTING}")

    def _leave(self) -> None:
        self._depth -= 1

    def _qualified_name(self, *, allow_leading_dot: bool = True) -> str:
        leading = self._accept(".") if allow_leading_dot else False
        first = self._expect_kind("ident", "identifier").value
        parts = [first]
        while self._accept("."):
            parts.append(self._expect_kind("ident", "identifier").value)
        return ("." if leading else "") + ".".join(parts)

    def _full_name(self, nesting: tuple[str, ...], name: str) -> str:
        parts = [part for part in (self.package, *nesting, name) if part]
        return "." + ".".join(parts)

    def _skip_bracket_options(self) -> None:
        if not self._accept("["):
            return
        depth = 1
        while depth:
            token = self._take()
            if token.kind == "eof":
                self._fail("unterminated field options", token)
            if token.value == "[":
                depth += 1
            elif token.value == "]":
                depth -= 1

    def _skip_statement(self) -> None:
        depth = 0
        while True:
            token = self._take()
            if token.kind == "eof":
                self._fail("unterminated statement", token)
            if token.value in "([{<":
                depth += 1
            elif token.value in ")]}>":
                if depth == 0:
                    self._fail("unbalanced statement", token)
                depth -= 1
            elif token.value == ";" and depth == 0:
                return

    def _skip_block_or_semicolon(self) -> None:
        if self._accept(";"):
            return
        self._expect("{")
        depth = 1
        while depth:
            token = self._take()
            if token.kind == "eof":
                self._fail("unterminated option block", token)
            if token.value == "{":
                depth += 1
            elif token.value == "}":
                depth -= 1
        self._accept(";")

    def _signed_int(self) -> int:
        sign = -1 if self._accept("-") else 1
        self._accept("+")
        return sign * int(self._expect_kind("number", "integer").value)

    def _field_number(self) -> int:
        value = self._signed_int()
        if (
            value < 1
            or value > _MAX_FIELD_NUMBER
            or _RESERVED_FIELD_MIN <= value <= _RESERVED_FIELD_MAX
        ):
            self._fail(f"invalid protobuf field number {value}")
        return value

    def _type_name(self) -> str:
        if self._accept("map"):
            self._expect("<")
            key = self._qualified_name()
            self._expect(",")
            value = self._qualified_name()
            self._expect(">")
            return f"map<{key},{value}>"
        return self._qualified_name()

    def _reserved(self) -> tuple[list[tuple[int, int]], list[str]]:
        ranges: list[tuple[int, int]] = []
        names: list[str] = []
        self._expect("reserved")
        while True:
            if self.current.kind == "string":
                names.append(self._take().value)
            else:
                start = self._signed_int()
                end = start
                if self._accept("to"):
                    if self._accept("max"):
                        end = _MAX_FIELD_NUMBER
                    else:
                        end = self._signed_int()
                if start > end:
                    self._fail(f"invalid reserved range {start} to {end}")
                ranges.append((start, end))
            if not self._accept(","):
                break
        self._expect(";")
        return ranges, names

    def _field(self, *, oneof_name: str | None = None) -> ProtoField:
        cardinality = "oneof" if oneof_name else "singular"
        if oneof_name is None and self.current.value in {"optional", "required", "repeated"}:
            cardinality = self._take().value
        if self.current.value == "group":
            self._fail("group fields are not supported by deterministic protobuf analysis")
        type_name = self._type_name()
        name = self._expect_kind("ident", "field name").value
        self._expect("=")
        number = self._field_number()
        self._skip_bracket_options()
        self._expect(";")
        if type_name.startswith("map<"):
            cardinality = "map"
        if oneof_name is not None:
            cardinality = f"oneof:{oneof_name}"
        return ProtoField(name=name, number=number, type_name=type_name, cardinality=cardinality)

    def _oneof(self) -> list[ProtoField]:
        self._expect("oneof")
        name = self._expect_kind("ident", "oneof name").value
        self._expect("{")
        self._enter()
        fields: list[ProtoField] = []
        try:
            while not self._accept("}"):
                if self.current.kind == "eof":
                    self._fail("unterminated oneof")
                if self._accept(";"):
                    continue
                if self.current.value == "option":
                    self._skip_statement()
                    continue
                fields.append(self._field(oneof_name=name))
        finally:
            self._leave()
        return fields

    def _message(self, nesting: tuple[str, ...]) -> None:
        self._expect("message")
        name = self._expect_kind("ident", "message name").value
        full_name = self._full_name(nesting, name)
        self._expect("{")
        self._enter()
        fields: list[ProtoField] = []
        ranges: list[tuple[int, int]] = []
        names: list[str] = []
        child_nesting = (*nesting, name)
        try:
            while not self._accept("}"):
                if self.current.kind == "eof":
                    self._fail(f"unterminated message {full_name}")
                if self._accept(";"):
                    continue
                if self.current.value == "message":
                    self._message(child_nesting)
                elif self.current.value == "enum":
                    self._enum(child_nesting)
                elif self.current.value == "oneof":
                    fields.extend(self._oneof())
                elif self.current.value == "reserved":
                    item_ranges, item_names = self._reserved()
                    ranges.extend(item_ranges)
                    names.extend(item_names)
                elif self.current.value == "option":
                    self._skip_statement()
                elif self.current.value in {"extensions", "extend"}:
                    self._fail(
                        f"{self.current.value} is not supported by deterministic protobuf analysis"
                    )
                else:
                    fields.append(self._field())
        finally:
            self._leave()
        self.messages.append(
            ProtoMessage(
                full_name=full_name,
                fields=tuple(sorted(fields, key=lambda item: (item.number, item.name))),
                reserved_ranges=tuple(sorted(set(ranges))),
                reserved_names=tuple(sorted(set(names))),
            )
        )

    def _enum(self, nesting: tuple[str, ...]) -> None:
        self._expect("enum")
        name = self._expect_kind("ident", "enum name").value
        full_name = self._full_name(nesting, name)
        self._expect("{")
        self._enter()
        values: list[ProtoEnumValue] = []
        ranges: list[tuple[int, int]] = []
        names: list[str] = []
        allow_alias = False
        try:
            while not self._accept("}"):
                if self.current.kind == "eof":
                    self._fail(f"unterminated enum {full_name}")
                if self._accept(";"):
                    continue
                if self.current.value == "reserved":
                    item_ranges, item_names = self._reserved()
                    ranges.extend(item_ranges)
                    names.extend(item_names)
                    continue
                if self.current.value == "option":
                    self._expect("option")
                    option_name = self._qualified_name(allow_leading_dot=False)
                    self._expect("=")
                    if option_name == "allow_alias" and self.current.value in {"true", "false"}:
                        allow_alias = self._take().value == "true"
                        self._expect(";")
                    else:
                        while not self._accept(";"):
                            if self.current.kind == "eof":
                                self._fail("unterminated enum option")
                            self._take()
                    continue
                value_name = self._expect_kind("ident", "enum value name").value
                self._expect("=")
                number = self._signed_int()
                self._skip_bracket_options()
                self._expect(";")
                values.append(ProtoEnumValue(name=value_name, number=number))
        finally:
            self._leave()
        self.enums.append(
            ProtoEnum(
                full_name=full_name,
                values=tuple(sorted(values, key=lambda item: (item.number, item.name))),
                reserved_ranges=tuple(sorted(set(ranges))),
                reserved_names=tuple(sorted(set(names))),
                allow_alias=allow_alias,
            )
        )

    def _rpc(self) -> ProtoRpc:
        self._expect("rpc")
        name = self._expect_kind("ident", "RPC name").value
        self._expect("(")
        client_streaming = self._accept("stream")
        request_type = self._qualified_name()
        self._expect(")")
        self._expect("returns")
        self._expect("(")
        server_streaming = self._accept("stream")
        response_type = self._qualified_name()
        self._expect(")")
        self._skip_block_or_semicolon()
        return ProtoRpc(
            name=name,
            request_type=request_type,
            response_type=response_type,
            client_streaming=client_streaming,
            server_streaming=server_streaming,
        )

    def _service(self) -> None:
        self._expect("service")
        name = self._expect_kind("ident", "service name").value
        full_name = self._full_name((), name)
        self._expect("{")
        self._enter()
        rpcs: list[ProtoRpc] = []
        try:
            while not self._accept("}"):
                if self.current.kind == "eof":
                    self._fail(f"unterminated service {full_name}")
                if self._accept(";"):
                    continue
                if self.current.value == "option":
                    self._skip_statement()
                    continue
                if self.current.value != "rpc":
                    self._fail(f"unsupported service declaration {self.current.value!r}")
                rpcs.append(self._rpc())
        finally:
            self._leave()
        self.services.append(
            ProtoService(full_name=full_name, rpcs=tuple(sorted(rpcs, key=lambda item: item.name)))
        )

    def _syntax(self) -> None:
        self._expect("syntax")
        self._expect("=")
        token = self._expect_kind("string", "syntax string")
        if token.value not in {"proto2", "proto3"}:
            self._fail(f"unsupported protobuf syntax {token.value!r}", token)
        self.syntax = token.value
        self._expect(";")

    def _package(self) -> None:
        self._expect("package")
        self.package = self._qualified_name(allow_leading_dot=False)
        self._expect(";")

    def _import(self) -> None:
        self._expect("import")
        modifier = ""
        if self.current.value in {"public", "weak"}:
            modifier = self._take().value
        path = self._expect_kind("string", "import path").value
        self._expect(";")
        self.imports.append(ProtoImport(path=path, modifier=modifier))

    def parse(self) -> ProtoDocument:
        syntax_seen = False
        package_seen = False
        while self.current.kind != "eof":
            if self._accept(";"):
                continue
            if self.current.value == "syntax":
                if syntax_seen:
                    self._fail("syntax may be declared only once")
                syntax_seen = True
                self._syntax()
            elif self.current.value == "package":
                if package_seen:
                    self._fail("package may be declared only once")
                package_seen = True
                self._package()
            elif self.current.value == "import":
                self._import()
            elif self.current.value == "option":
                self._skip_statement()
            elif self.current.value == "message":
                self._message(())
            elif self.current.value == "enum":
                self._enum(())
            elif self.current.value == "service":
                self._service()
            elif self.current.value in {"extend", "extensions"}:
                self._fail(
                    f"{self.current.value} is not supported by deterministic protobuf analysis"
                )
            else:
                self._fail(f"unsupported top-level declaration {self.current.value!r}")

        return ProtoDocument(
            syntax=self.syntax,
            package=self.package,
            imports=tuple(sorted(self.imports, key=lambda item: (item.path, item.modifier))),
            messages=tuple(sorted(self.messages, key=lambda item: item.full_name)),
            enums=tuple(sorted(self.enums, key=lambda item: item.full_name)),
            services=tuple(sorted(self.services, key=lambda item: item.full_name)),
        )


def _contains(ranges: Sequence[tuple[int, int]], number: int) -> bool:
    return any(start <= number <= end for start, end in ranges)


def _range_set_contains(
    ranges: Sequence[tuple[int, int]], candidate: tuple[int, int]
) -> bool:
    start, end = candidate
    return any(existing_start <= start and end <= existing_end for existing_start, existing_end in ranges)


def _portable_import(path: str) -> str | None:
    if (
        not path
        or path != path.strip()
        or "\\" in path
        or "\x00" in path
        or path.startswith("/")
        or _IMPORT_SCHEME.match(path)
        or re.match(r"^[A-Za-z]:", path)
    ):
        return None
    parts = path.split("/")
    if (
        len(path.encode("utf-8")) > 4096
        or len(parts) > 64
        or any(part in {"", ".", ".."} for part in parts)
        or any(len(part.encode("utf-8")) > 255 for part in parts)
    ):
        return None
    return PurePosixPath(path).as_posix()


def _resolve_import(
    snapshot: ContractSnapshot,
    imported: ProtoImport,
    sources: Mapping[str, ContractSnapshot],
    *,
    importer_path: str | None = None,
) -> tuple[ContractSnapshot | None, ContractFinding | None]:
    normalized = _portable_import(imported.path)
    pointer = f"/imports/{imported.path}"
    if normalized is None:
        return None, _finding(
            snapshot,
            "SDAI-CONTRACT-PROTOBUF-007",
            f"unsafe protobuf import path: {imported.path!r}",
            pointer=pointer,
        )

    parent = PurePosixPath(importer_path or snapshot.source.path).parent
    candidates = [normalized]
    relative = (parent / normalized).as_posix()
    if relative != normalized:
        candidates.append(relative)
    matches: dict[str, ContractSnapshot] = {}
    for candidate in candidates:
        target = sources.get(candidate)
        if target is not None:
            matches[target.source.path] = target
    if not matches:
        return None, _finding(
            snapshot,
            "SDAI-CONTRACT-PROTOBUF-008",
            f"protobuf import is not explicitly declared: {normalized}",
            pointer=pointer,
        )
    if len(matches) > 1:
        return None, _finding(
            snapshot,
            "SDAI-CONTRACT-PROTOBUF-009",
            f"protobuf import is ambiguous across declared sources: {normalized}",
            pointer=pointer,
        )
    return next(iter(matches.values())), None


def _validate_document(
    snapshot: ContractSnapshot,
    document: ProtoDocument,
    sources: Mapping[str, ContractSnapshot],
    *,
    importer_path: str | None = None,
) -> list[ContractFinding]:
    findings: list[ContractFinding] = []

    declaration_names: dict[str, str] = {}
    for kind, names in (
        ("message", [item.full_name for item in document.messages]),
        ("enum", [item.full_name for item in document.enums]),
        ("service", [item.full_name for item in document.services]),
    ):
        for name in names:
            prior = declaration_names.get(name)
            if prior is not None:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-PROTOBUF-003",
                        f"duplicate protobuf declaration {name}: {prior} and {kind}",
                        pointer=_symbol_pointer(f"{kind}s", name),
                    )
                )
            else:
                declaration_names[name] = kind

    import_paths: set[str] = set()
    for imported in document.imports:
        if imported.path in import_paths:
            findings.append(
                _finding(
                    snapshot,
                    "SDAI-CONTRACT-PROTOBUF-003",
                    f"duplicate protobuf import: {imported.path}",
                    pointer=f"/imports/{imported.path}",
                )
            )
        import_paths.add(imported.path)
        _, error = _resolve_import(
            snapshot,
            imported,
            sources,
            importer_path=importer_path,
        )
        if error is not None:
            findings.append(error)

    for message in document.messages:
        by_name: dict[str, ProtoField] = {}
        by_number: dict[int, ProtoField] = {}
        for field in message.fields:
            pointer = _symbol_pointer("messages", message.full_name, f"/fields/{field.number}")
            if field.name in by_name:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-PROTOBUF-005",
                        f"duplicate field name '{field.name}' in {message.full_name}",
                        pointer=pointer,
                    )
                )
            else:
                by_name[field.name] = field
            if field.number in by_number:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-PROTOBUF-005",
                        f"duplicate field number {field.number} in {message.full_name}",
                        pointer=pointer,
                    )
                )
            else:
                by_number[field.number] = field
            if _contains(message.reserved_ranges, field.number) or field.name in message.reserved_names:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-PROTOBUF-006",
                        f"field '{field.name}' uses a reserved number or name in {message.full_name}",
                        pointer=pointer,
                    )
                )

    for enum in document.enums:
        by_name: set[str] = set()
        by_number: set[int] = set()
        for value in enum.values:
            pointer = _symbol_pointer("enums", enum.full_name, f"/values/{value.number}")
            if value.name in by_name:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-PROTOBUF-005",
                        f"duplicate enum value name '{value.name}' in {enum.full_name}",
                        pointer=pointer,
                    )
                )
            by_name.add(value.name)
            if value.number in by_number and not enum.allow_alias:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-PROTOBUF-005",
                        f"duplicate enum number {value.number} without allow_alias in {enum.full_name}",
                        pointer=pointer,
                    )
                )
            by_number.add(value.number)
            if _contains(enum.reserved_ranges, value.number) or value.name in enum.reserved_names:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-PROTOBUF-006",
                        f"enum value '{value.name}' uses a reserved number or name in {enum.full_name}",
                        pointer=pointer,
                    )
                )
    for service in document.services:
        names: set[str] = set()
        for rpc in service.rpcs:
            if rpc.name in names:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-PROTOBUF-005",
                        f"duplicate RPC '{rpc.name}' in {service.full_name}",
                        pointer=_symbol_pointer("services", service.full_name, f"/rpcs/{rpc.name}"),
                    )
                )
            names.add(rpc.name)

    return findings


def _parse(
    snapshot: ContractSnapshot,
    sources: Mapping[str, ContractSnapshot],
    *,
    importer_path: str | None = None,
) -> _Parsed:
    try:
        document = _Parser(_tokenize(snapshot.text)).parse()
    except _ProtoParseError as exc:
        return _Parsed(
            None,
            (
                _finding(
                    snapshot,
                    "SDAI-CONTRACT-PROTOBUF-001",
                    str(exc),
                ),
            ),
        )
    findings = _validate_document(
        snapshot,
        document,
        sources,
        importer_path=importer_path,
    )
    findings.append(
        _finding(
            snapshot,
            "SDAI-CONTRACT-PROTOBUF-000",
            f"effective protobuf syntax: {document.syntax}",
            pointer="/syntax",
            severity=ContractSeverity.INFO,
        )
    )
    return _Parsed(
        document,
        tuple(
            sorted(
                findings,
                key=lambda item: (
                    item.severity.value,
                    item.code,
                    item.provenance.pointer if item.provenance and item.provenance.pointer else "",
                    item.message,
                ),
            )
        ),
    )


def _diff_reserved_surface(
    before_ranges: Sequence[tuple[int, int]],
    after_ranges: Sequence[tuple[int, int]],
    before_names: Sequence[str],
    after_names: Sequence[str],
    *,
    snapshot: ContractSnapshot,
    pointer: str,
    label: str,
    direction: CompatibilityDirection,
) -> list[ContractFinding]:
    findings: list[ContractFinding] = []
    for reserved_range in before_ranges:
        if not _range_set_contains(after_ranges, reserved_range):
            findings.append(
                _finding(
                    snapshot,
                    "SDAI-CONTRACT-PROTOBUF-DIFF-006",
                    f"{label} reserved range {reserved_range[0]}..{reserved_range[1]} was removed or narrowed",
                    pointer=f"{pointer}/reserved",
                    compatibility=direction,
                )
            )
    for name in sorted(set(before_names) - set(after_names)):
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-PROTOBUF-DIFF-006",
                f"{label} reserved name '{name}' was removed",
                pointer=f"{pointer}/reserved",
                compatibility=direction,
            )
        )
    return findings


def _one_way_diff(
    before: ProtoDocument,
    after: ProtoDocument,
    *,
    snapshot: ContractSnapshot,
    direction: CompatibilityDirection,
) -> list[ContractFinding]:
    findings: list[ContractFinding] = []
    if before.package != after.package:
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-PROTOBUF-DIFF-013",
                f"protobuf package changed from {before.package!r} to {after.package!r}",
                pointer="/package",
                compatibility=direction,
            )
        )
    if before.syntax != after.syntax:
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-PROTOBUF-DIFF-014",
                f"protobuf syntax changed from {before.syntax} to {after.syntax}",
                pointer="/syntax",
                compatibility=direction,
            )
        )

    before_public = {item.path for item in before.imports if item.modifier == "public"}
    after_public = {item.path for item in after.imports if item.modifier == "public"}
    for path in sorted(before_public - after_public):
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-PROTOBUF-DIFF-015",
                f"public protobuf import was removed or downgraded: {path}",
                pointer=f"/imports/{path}",
                compatibility=direction,
            )
        )

    old_messages = {item.full_name: item for item in before.messages}
    new_messages = {item.full_name: item for item in after.messages}
    for name in sorted(old_messages.keys() - new_messages.keys()):
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-PROTOBUF-DIFF-001",
                f"message removed: {name}",
                pointer=_symbol_pointer("messages", name),
                compatibility=direction,
            )
        )
    for name in sorted(old_messages.keys() & new_messages.keys()):
        old = old_messages[name]
        new = new_messages[name]
        pointer = _symbol_pointer("messages", name)
        findings.extend(
            _diff_reserved_surface(
                old.reserved_ranges,
                new.reserved_ranges,
                old.reserved_names,
                new.reserved_names,
                snapshot=snapshot,
                pointer=pointer,
                label=f"message {name}",
                direction=direction,
            )
        )
        old_numbers = {item.number: item for item in old.fields}
        new_numbers = {item.number: item for item in new.fields}
        new_names = {item.name: item for item in new.fields}

        for number in sorted(old_numbers):
            old_field = old_numbers[number]
            new_field = new_numbers.get(number)
            field_pointer = f"{pointer}/fields/{number}"
            if new_field is None:
                moved = new_names.get(old_field.name)
                if moved is not None:
                    findings.append(
                        _finding(
                            snapshot,
                            "SDAI-CONTRACT-PROTOBUF-DIFF-003",
                            f"field '{old_field.name}' moved from number {number} to {moved.number}",
                            pointer=field_pointer,
                            compatibility=direction,
                        )
                    )
                elif not _contains(new.reserved_ranges, number):
                    findings.append(
                        _finding(
                            snapshot,
                            "SDAI-CONTRACT-PROTOBUF-DIFF-002",
                            f"field number {number} ('{old_field.name}') was removed without reservation",
                            pointer=field_pointer,
                            compatibility=direction,
                        )
                    )
                continue
            if new_field.name != old_field.name:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-PROTOBUF-DIFF-004",
                        f"field number {number} was reused from '{old_field.name}' to '{new_field.name}'",
                        pointer=field_pointer,
                        compatibility=direction,
                    )
                )
            if (
                new_field.type_name != old_field.type_name
                or new_field.cardinality != old_field.cardinality
            ):
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-PROTOBUF-DIFF-005",
                        f"field {number} changed type/cardinality from "
                        f"{old_field.type_name}/{old_field.cardinality} to "
                        f"{new_field.type_name}/{new_field.cardinality}",
                        pointer=field_pointer,
                        compatibility=direction,
                    )
                )

        for number in sorted(new_numbers.keys() - old_numbers.keys()):
            field = new_numbers[number]
            field_pointer = f"{pointer}/fields/{number}"
            if _contains(old.reserved_ranges, number) or field.name in old.reserved_names:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-PROTOBUF-DIFF-006",
                        f"new field '{field.name}' violates the previous reserved surface",
                        pointer=field_pointer,
                        compatibility=direction,
                    )
                )
            if field.cardinality == "required":
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-PROTOBUF-DIFF-005",
                        f"required field '{field.name}' was added",
                        pointer=field_pointer,
                        compatibility=direction,
                    )
                )

    old_enums = {item.full_name: item for item in before.enums}
    new_enums = {item.full_name: item for item in after.enums}
    for name in sorted(old_enums.keys() - new_enums.keys()):
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-PROTOBUF-DIFF-007",
                f"enum removed: {name}",
                pointer=_symbol_pointer("enums", name),
                compatibility=direction,
            )
        )
    for name in sorted(old_enums.keys() & new_enums.keys()):
        old = old_enums[name]
        new = new_enums[name]
        pointer = _symbol_pointer("enums", name)
        findings.extend(
            _diff_reserved_surface(
                old.reserved_ranges,
                new.reserved_ranges,
                old.reserved_names,
                new.reserved_names,
                snapshot=snapshot,
                pointer=pointer,
                label=f"enum {name}",
                direction=direction,
            )
        )
        old_numbers = {item.number: item for item in old.values}
        new_numbers = {item.number: item for item in new.values}
        new_names = {item.name: item for item in new.values}
        for number in sorted(old_numbers):
            old_value = old_numbers[number]
            new_value = new_numbers.get(number)
            value_pointer = f"{pointer}/values/{number}"
            if new_value is None:
                moved = new_names.get(old_value.name)
                if moved is not None:
                    findings.append(
                        _finding(
                            snapshot,
                            "SDAI-CONTRACT-PROTOBUF-DIFF-009",
                            f"enum value '{old_value.name}' moved from {number} to {moved.number}",
                            pointer=value_pointer,
                            compatibility=direction,
                        )
                    )
                elif not _contains(new.reserved_ranges, number):
                    findings.append(
                        _finding(
                            snapshot,
                            "SDAI-CONTRACT-PROTOBUF-DIFF-008",
                            f"enum number {number} ('{old_value.name}') was removed without reservation",
                            pointer=value_pointer,
                            compatibility=direction,
                        )
                    )
            elif new_value.name != old_value.name:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-PROTOBUF-DIFF-009",
                        f"enum number {number} was reused from '{old_value.name}' to '{new_value.name}'",
                        pointer=value_pointer,
                        compatibility=direction,
                    )
                )
        for number in sorted(new_numbers.keys() - old_numbers.keys()):
            value = new_numbers[number]
            if _contains(old.reserved_ranges, number) or value.name in old.reserved_names:
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-PROTOBUF-DIFF-006",
                        f"new enum value '{value.name}' violates the previous reserved surface",
                        pointer=f"{pointer}/values/{number}",
                        compatibility=direction,
                    )
                )

    old_services = {item.full_name: item for item in before.services}
    new_services = {item.full_name: item for item in after.services}
    for name in sorted(old_services.keys() - new_services.keys()):
        findings.append(
            _finding(
                snapshot,
                "SDAI-CONTRACT-PROTOBUF-DIFF-010",
                f"service removed: {name}",
                pointer=_symbol_pointer("services", name),
                compatibility=direction,
            )
        )
    for name in sorted(old_services.keys() & new_services.keys()):
        old_rpcs = {item.name: item for item in old_services[name].rpcs}
        new_rpcs = {item.name: item for item in new_services[name].rpcs}
        pointer = _symbol_pointer("services", name)
        for rpc_name in sorted(old_rpcs.keys() - new_rpcs.keys()):
            findings.append(
                _finding(
                    snapshot,
                    "SDAI-CONTRACT-PROTOBUF-DIFF-011",
                    f"RPC removed: {name}.{rpc_name}",
                    pointer=f"{pointer}/rpcs/{rpc_name}",
                    compatibility=direction,
                )
            )
        for rpc_name in sorted(old_rpcs.keys() & new_rpcs.keys()):
            old_rpc, new_rpc = old_rpcs[rpc_name], new_rpcs[rpc_name]
            if (
                old_rpc.request_type != new_rpc.request_type
                or old_rpc.response_type != new_rpc.response_type
                or old_rpc.client_streaming != new_rpc.client_streaming
                or old_rpc.server_streaming != new_rpc.server_streaming
            ):
                findings.append(
                    _finding(
                        snapshot,
                        "SDAI-CONTRACT-PROTOBUF-DIFF-012",
                        f"RPC signature changed: {name}.{rpc_name}",
                        pointer=f"{pointer}/rpcs/{rpc_name}",
                        compatibility=direction,
                    )
                )

    return findings


class ProtobufContractAdapter:
    kind = "protobuf"

    def __init__(self, sources: Sequence[ContractSnapshot] = ()) -> None:
        self._sources = {
            item.source.path: item
            for item in sources
            if item.source.kind == "protobuf"
        }

    def _source_index(self, *overlays: ContractSnapshot) -> dict[str, ContractSnapshot]:
        sources = dict(self._sources)
        for snapshot in overlays:
            if snapshot.source.kind == "protobuf":
                sources[snapshot.source.path] = snapshot
        return sources

    def check(self, snapshot: ContractSnapshot) -> Sequence[ContractFinding]:
        return _parse(snapshot, self._source_index(snapshot)).findings

    def diff(
        self,
        before: ContractSnapshot,
        after: ContractSnapshot,
        direction: CompatibilityDirection,
    ) -> Sequence[ContractFinding]:
        sources = self._source_index(before, after)
        baseline = _parse(before, sources, importer_path=before.source.path)
        candidate = _parse(after, sources, importer_path=before.source.path)
        all_parse_findings = [*baseline.findings, *candidate.findings]
        if (
            baseline.document is None
            or candidate.document is None
            or any(item.severity is ContractSeverity.ERROR for item in all_parse_findings)
        ):
            return tuple(all_parse_findings)

        if direction is CompatibilityDirection.FORWARD:
            return tuple(
                _one_way_diff(
                    candidate.document,
                    baseline.document,
                    snapshot=before,
                    direction=CompatibilityDirection.FORWARD,
                )
            )
        if direction is CompatibilityDirection.FULL:
            return tuple(
                [
                    *_one_way_diff(
                        baseline.document,
                        candidate.document,
                        snapshot=after,
                        direction=CompatibilityDirection.BACKWARD,
                    ),
                    *_one_way_diff(
                        candidate.document,
                        baseline.document,
                        snapshot=before,
                        direction=CompatibilityDirection.FORWARD,
                    ),
                ]
            )
        return tuple(
            _one_way_diff(
                baseline.document,
                candidate.document,
                snapshot=after,
                direction=CompatibilityDirection.BACKWARD,
            )
        )
