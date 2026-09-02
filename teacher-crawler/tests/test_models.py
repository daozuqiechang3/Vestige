from crawler.models import Teacher


def test_teacher_has_the_canonical_fields() -> None:
    assert set(Teacher.model_fields) == {
        "school",
        "college",
        "name",
        "category",
        "title",
        "email",
        "phone",
        "department",
        "research_interests",
        "admission_info",
        "sections",
        "full_text",
        "photo_url",
        "profile_url",
        "collected_at",
    }
