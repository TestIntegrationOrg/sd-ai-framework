from __future__ import annotations

from pathlib import Path

from sdai.contracts import (
    CompatibilityDirection,
    ContractAdapterRegistry,
    ContractSource,
    check_contract,
    diff_contracts,
    load_contract_snapshot,
)
from sdai.openapi_contracts import OpenAPIContractAdapter


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _snapshot(tmp_path: Path, name: str, text: str):
    root = tmp_path
    path = f"contracts/{name}.yaml"
    _write(root / path, text)
    return load_contract_snapshot(root, ContractSource(source_id=name, kind="openapi", path=path))


def _doc(paths: str, components: str = "") -> str:
    return f"""openapi: 3.1.0
info:
  title: Test API
  version: 1.0.0
paths:
{paths}
{components}"""


def _registry():
    return ContractAdapterRegistry([OpenAPIContractAdapter()])


def test_valid_openapi_is_accepted(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, "api", _doc("""  /pets:
    get:
      operationId: listPets
      responses:
        '200':
          description: ok
"""))
    result = check_contract(snapshot, _registry())
    assert result.valid
    assert result.findings == ()


def test_malformed_and_unsupported_openapi_have_stable_findings(tmp_path: Path) -> None:
    malformed = _snapshot(tmp_path, "bad", "openapi: [\n")
    result = check_contract(malformed, _registry())
    assert [item.code for item in result.findings] == ["SDAI-CONTRACT-OPENAPI-001"]
    unsupported = _snapshot(tmp_path, "v2", "swagger: '2.0'\ninfo: {title: x, version: '1'}\npaths: {}\n")
    codes = {item.code for item in check_contract(unsupported, _registry()).findings}
    assert "SDAI-CONTRACT-OPENAPI-003" in codes


def test_external_and_unresolved_refs_are_rejected_without_network(tmp_path: Path) -> None:
    external = _snapshot(tmp_path, "external", _doc("""  /pets:
    get:
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                $ref: https://example.invalid/schema.yaml
"""))
    codes = {item.code for item in check_contract(external, _registry()).findings}
    assert "SDAI-CONTRACT-OPENAPI-007" in codes
    unresolved = _snapshot(tmp_path, "missing", _doc("""  /pets:
    get:
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Missing'
"""))
    codes = {item.code for item in check_contract(unresolved, _registry()).findings}
    assert "SDAI-CONTRACT-OPENAPI-008" in codes


def test_local_ref_and_path_parameter_validation(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, "refs", _doc("""  /pets/{id}:
    get:
      parameters:
        - $ref: '#/components/parameters/PetId'
      responses:
        '200':
          description: ok
""", """components:
  parameters:
    PetId:
      name: id
      in: path
      required: true
      schema:
        type: string
"""))
    assert check_contract(snapshot, _registry()).valid


def test_operation_removal_is_breaking(tmp_path: Path) -> None:
    before = _snapshot(tmp_path, "before", _doc("""  /pets:
    get:
      responses:
        '200': {description: ok}
        '404': {description: missing}
"""))
    after = _snapshot(tmp_path, "after", _doc("""  /pets:
    post:
      responses:
        '200': {description: ok}
"""))
    result = diff_contracts(before, after, _registry())
    codes = {item.code for item in result.findings}
    assert "SDAI-CONTRACT-OPENAPI-DIFF-001" in codes
    assert not result.compatible


def test_required_parameter_added_is_breaking(tmp_path: Path) -> None:
    before = _snapshot(tmp_path, "before", _doc("""  /pets:
    get:
      responses:
        '200': {description: ok}
"""))
    after = _snapshot(tmp_path, "after", _doc("""  /pets:
    get:
      parameters:
        - name: limit
          in: query
          required: true
          schema: {type: integer}
      responses:
        '200': {description: ok}
"""))
    codes = {item.code for item in diff_contracts(before, after, _registry()).findings}
    assert "SDAI-CONTRACT-OPENAPI-DIFF-010" in codes


def test_request_enum_narrowing_is_breaking(tmp_path: Path) -> None:
    before = _snapshot(tmp_path, "before", _doc("""  /pets:
    get:
      parameters:
        - name: state
          in: query
          schema: {type: string, enum: [active, inactive]}
      responses:
        '200': {description: ok}
"""))
    after = _snapshot(tmp_path, "after", _doc("""  /pets:
    get:
      parameters:
        - name: state
          in: query
          schema: {type: string, enum: [active]}
      responses:
        '200': {description: ok}
"""))
    codes = {item.code for item in diff_contracts(before, after, _registry()).findings}
    assert "SDAI-CONTRACT-OPENAPI-DIFF-022" in codes


def test_response_schema_property_removal_and_enum_expansion_are_breaking(tmp_path: Path) -> None:
    before = _snapshot(tmp_path, "before", _doc("""  /pets:
    get:
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                type: object
                required: [id, state]
                properties:
                  id: {type: string}
                  state: {type: string, enum: [active]}
"""))
    after = _snapshot(tmp_path, "after", _doc("""  /pets:
    get:
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                type: object
                required: [state]
                properties:
                  state: {type: string, enum: [active, inactive]}
"""))
    codes = {item.code for item in diff_contracts(before, after, _registry()).findings}
    assert "SDAI-CONTRACT-OPENAPI-DIFF-023" in codes
    assert "SDAI-CONTRACT-OPENAPI-DIFF-025" in codes
    assert "SDAI-CONTRACT-OPENAPI-DIFF-026" in codes


def test_optional_parameter_and_operation_addition_is_non_breaking(tmp_path: Path) -> None:
    before = _snapshot(tmp_path, "before", _doc("""  /pets:
    get:
      responses:
        '200': {description: ok}
"""))
    after = _snapshot(tmp_path, "after", _doc("""  /pets:
    get:
      parameters:
        - name: limit
          in: query
          required: false
          schema: {type: integer}
      responses:
        '200': {description: ok}
    post:
      responses:
        '201': {description: created}
"""))
    result = diff_contracts(before, after, _registry())
    assert result.compatible
    assert result.findings == ()


def test_findings_are_byte_stable(tmp_path: Path) -> None:
    before = _snapshot(tmp_path, "before", _doc("""  /pets:
    get:
      responses:
        '200': {description: ok}
"""))
    after = _snapshot(tmp_path, "after", _doc("""  /pets: {}
"""))
    left = diff_contracts(before, after, _registry(), CompatibilityDirection.BACKWARD)
    right = diff_contracts(before, after, _registry(), CompatibilityDirection.BACKWARD)
    assert left.to_json() == right.to_json()
