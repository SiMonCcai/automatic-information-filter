import pytest

from pipeline.deepseek_enrichment import _parse_score_response


def test_score_parser_rejects_json_boolean():
    with pytest.raises(ValueError, match="integer 1-5"):
        _parse_score_response('{"score": true}', "实用性")
