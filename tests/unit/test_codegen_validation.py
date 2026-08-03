"""Semantic checks over generated output: contract structure, syntax, coverage."""

from __future__ import annotations

from forge.codegen_validation import (
    compile_python_sources,
    derive_operation_coverage,
    operations,
    validate_openapi,
)


def _spec(**overrides):
    base = {
        "openapi": "3.0.3",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {
            "/v1/items": {
                "post": {
                    "operationId": "createItem",
                    "responses": {"201": {"description": "created"}},
                }
            }
        },
    }
    base.update(overrides)
    return base


class TestOpenAPIStructure:
    def test_a_well_formed_contract_has_no_errors(self):
        assert validate_openapi(_spec()) == []

    def test_non_object_document_is_rejected(self):
        assert validate_openapi("not a spec") == ["OpenAPI document is not a JSON object"]

    def test_missing_version_is_reported(self):
        errors = validate_openapi(_spec(openapi=None))
        assert any("openapi' version" in e for e in errors)

    def test_swagger_2_is_rejected(self):
        errors = validate_openapi(_spec(openapi="2.0"))
        assert any("unsupported" in e for e in errors)

    def test_empty_paths_is_rejected(self):
        errors = validate_openapi(_spec(paths={}))
        assert errors == ["document declares no paths"]

    def test_missing_info_fields_are_reported(self):
        errors = validate_openapi(_spec(info={"title": "", "version": "1.0"}))
        assert "info.title is empty" in errors

    def test_operation_without_responses_is_rejected(self):
        spec = _spec(paths={"/v1/items": {"get": {"operationId": "list"}}})
        errors = validate_openapi(spec)
        assert any("no responses defined" in e for e in errors)

    def test_path_must_start_with_slash(self):
        spec = _spec(paths={"v1/items": {"get": {"responses": {"200": {}}}}})
        errors = validate_openapi(spec)
        assert any("must start with '/'" in e for e in errors)

    def test_undeclared_path_parameter_is_caught(self):
        """A {code} in the template with no matching parameter breaks codegen."""
        spec = _spec(paths={"/{code}": {"get": {"responses": {"302": {}}}}})
        errors = validate_openapi(spec)
        assert any("path parameter 'code' is not declared" in e for e in errors)

    def test_declared_path_parameter_passes(self):
        spec = _spec(
            paths={
                "/{code}": {
                    "get": {
                        "parameters": [{"name": "code", "in": "path", "required": True}],
                        "responses": {"302": {"description": "redirect"}},
                    }
                }
            }
        )
        assert validate_openapi(spec) == []

    def test_duplicate_operation_ids_are_caught(self):
        spec = _spec(
            paths={
                "/a": {"get": {"operationId": "dup", "responses": {"200": {}}}},
                "/b": {"get": {"operationId": "dup", "responses": {"200": {}}}},
            }
        )
        errors = validate_openapi(spec)
        assert any("duplicate operationId" in e for e in errors)

    def test_dangling_ref_is_caught(self):
        spec = _spec(
            paths={
                "/a": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/Missing"}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        )
        errors = validate_openapi(spec)
        assert any("unresolvable $ref" in e for e in errors)

    def test_resolvable_ref_passes(self):
        spec = _spec(
            paths={
                "/a": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/Item"}
                                    }
                                }
                            }
                        }
                    }
                }
            },
            components={"schemas": {"Item": {"type": "object"}}},
        )
        assert validate_openapi(spec) == []

    def test_invalid_status_code_is_caught(self):
        spec = _spec(paths={"/a": {"get": {"responses": {"twohundred": {}}}}})
        errors = validate_openapi(spec)
        assert any("invalid response code" in e for e in errors)


class TestOperationEnumeration:
    def test_lists_method_and_path_pairs(self):
        spec = _spec(
            paths={
                "/a": {"get": {"responses": {"200": {}}}, "post": {"responses": {"201": {}}}},
                "/b": {"delete": {"responses": {"204": {}}}},
            }
        )
        assert operations(spec) == ["DELETE /b", "GET /a", "POST /a"]

    def test_ignores_non_method_keys(self):
        spec = _spec(paths={"/a": {"parameters": [], "get": {"responses": {"200": {}}}}})
        assert operations(spec) == ["GET /a"]

    def test_handles_a_malformed_document(self):
        assert operations(None) == []


class TestPythonCompilation:
    def test_valid_sources_produce_no_errors(self, tmp_path):
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "main.py").write_text("def go():\n    return 1\n")

        assert compile_python_sources(tmp_path) == []

    def test_syntax_error_is_reported_with_location(self, tmp_path):
        (tmp_path / "broken.py").write_text("def go(:\n    pass\n")

        errors = compile_python_sources(tmp_path)

        assert len(errors) == 1
        assert "broken.py:1" in errors[0]

    def test_missing_workspace_is_an_error(self, tmp_path):
        errors = compile_python_sources(tmp_path / "nope")

        assert "does not exist" in errors[0]

    def test_vendored_directories_are_skipped(self, tmp_path):
        vendor = tmp_path / "node_modules"
        vendor.mkdir()
        (vendor / "bad.py").write_text("this is not python !!!")

        assert compile_python_sources(tmp_path) == []


class TestOperationCoverage:
    def _plan(self, api_cases):
        return {"api": api_cases, "unit": ["something unrelated"]}

    def test_full_coverage_when_every_path_is_named(self):
        spec = _spec(
            paths={
                "/v1/items": {"post": {"responses": {"201": {}}}},
                "/v1/items/{id}": {"get": {"responses": {"200": {}}}},
            }
        )
        plan = self._plan(["POST /v1/items contract", "GET /v1/items/{id} contract"])

        cov = derive_operation_coverage(spec, plan)

        assert cov["coverage_pct"] == 100.0
        assert cov["uncovered"] == []

    def test_uncovered_operations_are_named(self):
        spec = _spec(
            paths={
                "/v1/items": {"post": {"responses": {"201": {}}}},
                "/v1/orphan": {"get": {"responses": {"200": {}}}},
            }
        )
        plan = self._plan(["POST /v1/items contract"])

        cov = derive_operation_coverage(spec, plan)

        assert cov["coverage_pct"] == 50.0
        assert cov["uncovered"] == ["GET /v1/orphan"]

    def test_generic_case_names_do_not_count_as_coverage(self):
        """'OpenAPI conformance' is not evidence that any operation is tested."""
        spec = _spec()
        plan = self._plan(["OpenAPI conformance", "authz scope denials"])

        cov = derive_operation_coverage(spec, plan)

        assert cov["coverage_pct"] == 0.0

    def test_no_operations_yields_an_unmeasurable_result(self):
        cov = derive_operation_coverage({"openapi": "3.0.3", "paths": {}}, {"api": []})

        assert cov["coverage_pct"] is None
        assert cov["declared_operations"] == 0

    def test_malformed_plan_does_not_raise(self):
        cov = derive_operation_coverage(_spec(), ["not", "a", "dict"])

        assert cov["coverage_pct"] == 0.0
