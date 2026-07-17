import pytest

from app.db.seed import build_org_tree, canonical_store_names, infer_city, seed_org_tree
from app.models.org import OrgType, OrgUnit

pytestmark = pytest.mark.usefixtures("pg_engine")


def test_infer_city_by_prefix():
    assert infer_city("深圳海岸城店") == "深圳"
    assert infer_city("东莞里悦里店") == "东莞"
    assert infer_city("珠海某店") == "珠海"
    assert infer_city("桂城万达店") == "佛山"
    assert infer_city("长兴路总店") == "广州"  # 默认


def test_build_org_tree_groups_and_dedups():
    tree = build_org_tree(["深圳A店", "深圳A店", "广州B店", "东莞C店", " "])
    cities = {r.city for r in tree.regions}
    assert cities == {"深圳", "广州", "东莞"}
    sz = next(r for r in tree.regions if r.city == "深圳")
    assert sz.stores == ["深圳A店"]  # 去重
    assert tree.store_count == 3


def test_canonical_store_names_from_aliases():
    config = {"store_aliases": {"万科": "天河智慧城店", "富基路店": "富基广场店"}}
    names = canonical_store_names(config)
    assert "天河智慧城店" in names
    assert "富基广场店" in names


def test_seed_org_tree_persists_hierarchy(db_session):
    tree = build_org_tree(["深圳海岸城店", "长兴路总店", "东莞里悦里店"])
    created = seed_org_tree(db_session, tree)
    assert created == 3

    groups = db_session.query(OrgUnit).filter_by(type=OrgType.GROUP).all()
    stores = db_session.query(OrgUnit).filter_by(type=OrgType.STORE).all()
    assert len(groups) == 1
    assert len(stores) == 3
    # 每个门店都能上溯到集团
    for store in stores:
        assert store.parent.type is OrgType.REGION
        assert store.parent.parent.type is OrgType.GROUP


def test_seed_is_idempotent(db_session):
    tree = build_org_tree(["深圳海岸城店", "长兴路总店"])
    first = seed_org_tree(db_session, tree)
    second = seed_org_tree(db_session, tree)
    assert first == 2
    assert second == 0  # 再次种子不重复建门店
