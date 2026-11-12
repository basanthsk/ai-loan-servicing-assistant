from core.verify import numbers_in_text, unverified_numbers


def test_extracts_indian_and_plain_numbers():
    assert numbers_in_text("EMI ₹12,926 at 10.75% on 05 Nov, total ₹4,05,543") == [
        12926.0,
        10.75,
        5.0,
        405543.0,
    ]


def test_numbers_from_tool_results_pass_with_rounding():
    evidence = [{"current_emi": 12925.85, "rate": 10.75, "text": "₹4,05,543"}]
    assert (
        unverified_numbers("Your EMI is ₹12,926 at 10.75%. Outstanding ₹4,05,543.", evidence) == []
    )


def test_invented_number_is_flagged():
    evidence = [{"current_emi": 12925.85}]
    assert unverified_numbers("You would save ₹31,500 in interest.", evidence) == [31500.0]


def test_small_counts_and_customer_numbers_allowed():
    assert unverified_numbers("You had 3 failed payments.", []) == []
    assert unverified_numbers("A prepayment of ₹50,000 ...", ["if I prepay 50,000"]) == []


def test_identifiers_are_not_checked_as_numbers():
    text = "Dispute T\u20117781\u20112610\u201102 on loan L-1042, ref CDS-261111-0001 [LT-4.2] for ₹1,299."
    assert numbers_in_text(text) == [1299.0]
