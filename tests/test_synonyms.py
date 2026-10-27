import yaml

from ecom_agent_system.knowledge_base import SYNONYMS_PATH, chunk_text, load_corpus, load_synonyms


def test_repository_synonyms_are_well_formed():
    table = yaml.safe_load(SYNONYMS_PATH.read_text(encoding="utf-8"))
    for phrase, terms in table.items():
        assert isinstance(terms, list) and terms, phrase
        assert all(isinstance(term, str) and term for term in terms), phrase
        assert str(phrase) not in terms, phrase


def test_repository_synonyms_point_to_words_in_the_corpus():
    corpus = "\n".join(chunk_text(record) for record in load_corpus())
    synonyms = load_synonyms()
    missing = [(phrase, term) for phrase, terms in synonyms.items() for term in terms if term not in corpus]
    assert missing == []
