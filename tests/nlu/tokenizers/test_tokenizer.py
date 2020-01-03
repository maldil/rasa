import pytest

from rasa.nlu.constants import (
    CLS_TOKEN,
    TEXT_ATTRIBUTE,
    INTENT_ATTRIBUTE,
    RESPONSE_ATTRIBUTE,
    TOKENS_NAMES,
)
from rasa.nlu.training_data import Message, TrainingData
from rasa.nlu.tokenizers.whitespace_tokenizer import WhitespaceTokenizer


def test_tokens_comparison():
    from rasa.nlu.tokenizers.tokenizer import Token

    x = Token("hello", 0)
    y = Token("Hello", 0)

    assert x == x
    assert y < x

    assert x != 1

    with pytest.raises(TypeError):
        assert y < "a"


@pytest.mark.parametrize(
    "text, expected_tokens, expected_indices",
    [
        (
            "Forecast for lunch",
            ["Forecast", "for", "lunch", CLS_TOKEN],
            [(0, 8), (9, 12), (13, 18), (19, 26)],
        )
    ],
)
def test_train_tokenizer(text, expected_tokens, expected_indices):
    tk = WhitespaceTokenizer()

    message = Message(text)
    message.set(RESPONSE_ATTRIBUTE, text)
    message.set(INTENT_ATTRIBUTE, text)

    training_data = TrainingData()
    training_data.training_examples = [message]

    tk.train(training_data)

    for attribute in [RESPONSE_ATTRIBUTE, TEXT_ATTRIBUTE]:
        tokens = training_data.training_examples[0].get(TOKENS_NAMES[attribute])

        assert [t.text for t in tokens] == expected_tokens
        assert [t.start for t in tokens] == [i[0] for i in expected_indices]
        assert [t.end for t in tokens] == [i[1] for i in expected_indices]

    # check intent attribute
    tokens = training_data.training_examples[0].get(TOKENS_NAMES[INTENT_ATTRIBUTE])

    assert [t.text for t in tokens] == [text]


@pytest.mark.parametrize(
    "text, expected_tokens, expected_indices",
    [
        (
            "Forecast for lunch",
            ["Forecast", "for", "lunch", CLS_TOKEN],
            [(0, 8), (9, 12), (13, 18), (19, 26)],
        )
    ],
)
def test_process_tokenizer(text, expected_tokens, expected_indices):
    tk = WhitespaceTokenizer()

    message = Message(text)

    tk.process(message)

    tokens = message.get(TOKENS_NAMES[TEXT_ATTRIBUTE])

    assert [t.text for t in tokens] == expected_tokens
    assert [t.start for t in tokens] == [i[0] for i in expected_indices]
    assert [t.end for t in tokens] == [i[1] for i in expected_indices]
