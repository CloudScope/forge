"""
Generated-workspace correctness.

The contract is untrusted input to a code generator: OpenAPI names are free-form
strings and several legal ones are illegal Python.
"""

from __future__ import annotations

from forge.codegen_validation import compile_python_sources
from forge.workspace import _route_signature, _safe_ident, generate_fastapi_backend

SCHEMA = {"tables": {"items": ["id", "name"]}}


def _spec(paths):
    return {
        "openapi": "3.0.3",
        "info": {"title": "API", "version": "1.0.0"},
        "paths": paths,
    }


def _generate(tmp_path, spec):
    files, _ = generate_fastapi_backend(
        root=tmp_path,
        product="Probe",
        openapi=spec,
        schema_ddl=SCHEMA,
        hld={},
        lld={},
        security_findings=[],
        security_needs={"needed": []},
    )
    return files


class TestSafeIdent:
    def test_python_keywords_are_escaped(self):
        """Regression: a query parameter named `from` produced invalid syntax."""
        assert _safe_ident("from") == "from_"
        assert _safe_ident("class") == "class_"
        assert _safe_ident("return") == "return_"
        assert _safe_ident("import") == "import_"

    def test_soft_keywords_are_escaped(self):
        assert _safe_ident("match") == "match_"

    def test_illegal_characters_are_replaced(self):
        assert _safe_ident("user-id") == "user_id"
        assert _safe_ident("x.y") == "x_y"

    def test_leading_digits_are_prefixed(self):
        assert _safe_ident("2fa") == "t_2fa"

    def test_ordinary_names_are_unchanged(self):
        assert _safe_ident("short_code") == "short_code"

    def test_every_result_is_a_valid_identifier(self):
        for name in ("from", "class", "2fa", "user-id", "", "None", "lambda", "x.y"):
            assert _safe_ident(name).isidentifier()


class TestRouteSignature:
    def test_plain_parameters_need_no_alias(self):
        sig = _route_signature(
            [{"name": "id", "in": "path"}], [], has_body=False
        )

        assert sig == "db: DbSession, id: str"

    def test_keyword_query_parameter_is_aliased(self):
        sig = _route_signature(
            [], [{"name": "from", "in": "query"}], has_body=False
        )

        assert 'from_: str | None = Query(default=None, alias="from")' in sig

    def test_required_keyword_query_parameter_is_aliased(self):
        sig = _route_signature(
            [], [{"name": "from", "in": "query", "required": True}], has_body=False
        )

        assert 'from_: str = Query(..., alias="from")' in sig

    def test_keyword_path_parameter_is_aliased(self):
        sig = _route_signature(
            [{"name": "from", "in": "path"}], [], has_body=False
        )

        assert 'from_: str = Path(alias="from")' in sig

    def test_defaulted_parameters_come_last(self):
        """Python forbids a non-default argument after a defaulted one."""
        sig = _route_signature(
            [{"name": "from", "in": "path"}],
            [{"name": "cursor", "in": "query", "required": True}],
            has_body=False,
        )
        parts = [p.strip() for p in sig.split(",")]
        first_default = next(i for i, p in enumerate(parts) if "=" in p)

        assert all("=" in p for p in parts[first_default:])

    def test_body_parameter_leads_the_signature(self):
        sig = _route_signature([], [], has_body=True)

        assert sig.startswith("payload: dict[str, Any]")


class TestGeneratedBackendCompiles:
    def test_a_conventional_spec_generates_valid_python(self, tmp_path):
        _generate(
            tmp_path,
            _spec(
                {
                    "/v1/items": {
                        "post": {"operationId": "createItem", "responses": {"201": {}}},
                        "get": {"operationId": "listItems", "responses": {"200": {}}},
                    }
                }
            ),
        )

        assert compile_python_sources(tmp_path) == []

    def test_reserved_word_parameters_generate_valid_python(self, tmp_path):
        """The exact spec shape that broke the generator."""
        _generate(
            tmp_path,
            _spec(
                {
                    "/v1/links/{id}/analytics": {
                        "get": {
                            "operationId": "getLinkAnalytics",
                            "parameters": [
                                {"name": "id", "in": "path", "required": True},
                                {"name": "from", "in": "query"},
                                {"name": "to", "in": "query"},
                            ],
                            "responses": {"200": {}},
                        }
                    }
                }
            ),
        )

        assert compile_python_sources(tmp_path) == []

    def test_hostile_parameter_names_generate_valid_python(self, tmp_path):
        _generate(
            tmp_path,
            _spec(
                {
                    "/v1/things": {
                        "get": {
                            "operationId": "listThings",
                            "parameters": [
                                {"name": "class", "in": "query"},
                                {"name": "2fa", "in": "query"},
                                {"name": "user-id", "in": "query"},
                                {"name": "lambda", "in": "query", "required": True},
                            ],
                            "responses": {"200": {}},
                        }
                    }
                }
            ),
        )

        assert compile_python_sources(tmp_path) == []

    def test_an_empty_spec_still_produces_a_valid_app(self, tmp_path):
        _generate(tmp_path, _spec({}))

        assert compile_python_sources(tmp_path) == []

    def test_the_wire_contract_is_preserved_in_the_route(self, tmp_path):
        _generate(
            tmp_path,
            _spec(
                {
                    "/v1/report": {
                        "get": {
                            "operationId": "getReport",
                            "parameters": [{"name": "from", "in": "query"}],
                            "responses": {"200": {}},
                        }
                    }
                }
            ),
        )
        routes = next(tmp_path.rglob("routes_v1.py")).read_text()

        assert 'alias="from"' in routes, "renaming dropped the contract's wire name"
