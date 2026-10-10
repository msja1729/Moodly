"""Phase 2 track database: CSV import, the in-memory table, and the extended feature cache."""
import sqlite3

import numpy as np
import pytest

from backend.catalog import CatalogDB, CatalogTrack, TrackTable, load_table
from backend.features import FeatureCache, Features

HEADER = ("track_id,artists,track_name,track_genre,popularity,duration_ms,explicit,danceability,energy,key,"
          "loudness,mode,acousticness,valence,tempo\n")


def write_csv(path, rows):
    path.write_text(HEADER + "".join(rows), encoding="utf-8")
    return path


class TestImport:
    def test_import_merges_duplicate_rows_and_skips_bad_ones(self, tmp_path):
        csv_path = write_csv(tmp_path / "tracks.csv", [
            "t1,Ann;Bo,Song One,pop,50,200000,False,0.6,0.7,5,-6.5,1,0.2,0.8,121.0\n",
            "t1,Ann;Bo,Song One,dance,50,200000,False,0.6,0.7,5,-6.5,1,0.2,0.8,121.0\n",
            "t2,Cy,Song Two,rock,10,180000,True,0.4,0.9,0,-4.0,0,0.01,0.3,140.0\n",
            "t3,Dee,Broken,,0,0,False,,,,,,,,\n",
        ])
        db = CatalogDB(tmp_path / "catalog.db")
        assert db.import_csv(csv_path) == 2
        tracks = {t.id: t for t in db.load()}
        db.close()
        t1 = tracks["t1"]
        assert t1.artists == "Ann, Bo" and t1.genre == "pop, dance" and t1.popularity == 50
        assert t1.features == Features(0.8, 0.7, 0.2, 121.0, 5, 1, -6.5, 0.6)
        assert tracks["t2"].explicit and tracks["t2"].features.mode == 0

    def test_reimport_replaces(self, tmp_path):
        db = CatalogDB(tmp_path / "catalog.db")
        db.import_csv(write_csv(tmp_path / "a.csv", ["t1,A,S,pop,1,1,False,0.5,0.5,1,-5,1,0.5,0.5,100\n"]))
        db.import_csv(write_csv(tmp_path / "b.csv", ["t9,B,S,pop,1,1,False,0.5,0.5,1,-5,1,0.5,0.5,100\n"]))
        assert [t.id for t in db.load()] == ["t9"]
        db.close()

    def test_missing_catalog_is_an_empty_table(self, tmp_path):
        assert len(load_table(tmp_path / "none.db")) == 0


class TestTrackTable:
    def table(self):
        return TrackTable([CatalogTrack(f"t{i}", "S", "A", Features(i / 10, i / 10, 0.5)) for i in range(11)])

    def test_radius_search_nearest_first(self):
        hits = self.table().radius_search((0.5, 0.5), 0.15)
        assert [int(i) for i in hits] == [5, 4, 6]

    def test_radius_grows_until_enough(self):
        hits = self.table().radius_search((0.5, 0.5), 0.01, min_results=5)
        assert len(hits) >= 5 and int(hits[0]) == 5

    def test_mask(self):
        mask = np.ones(11, dtype=bool)
        mask[5] = False
        assert 5 not in self.table().radius_search((0.5, 0.5), 0.15, mask).tolist()

    def test_merge_marks_library_songs(self):
        base = self.table()
        merged = base.merged_with([CatalogTrack("t3", "Mine", "Me", Features(0.3, 0.3, 0.5), source="library"),
                                   CatalogTrack("new", "New", "Me", Features(0.9, 0.1, 0.5), source="library")])
        assert len(merged) == 12
        assert merged.get("t3").source == "library" and merged.get("t3").name == "S"  # dataset metadata kept
        assert merged.library.sum() == 2


class TestFeatureCacheMigration:
    def test_old_cache_gets_new_columns(self, tmp_path):
        path = tmp_path / "mood.db"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE features (track_id TEXT PRIMARY KEY, valence REAL, energy REAL,"
                   " acousticness REAL, source TEXT, fetched_at REAL)")
        db.execute("INSERT INTO features VALUES ('old', 0.1, 0.2, 0.3, 'csv', 0)")
        db.commit()
        db.close()

        cache = FeatureCache(path)
        assert cache.get_many(["old"])["old"] == Features(0.1, 0.2, 0.3)
        full = Features(0.5, 0.6, 0.7, 128.0, 9, 0, -7.5, 0.8)
        cache.put_many({"new": full}, "reccobeats")
        assert cache.get_many(["new"])["new"] == full
        cache.close()


@pytest.mark.parametrize("row, expect", [
    ({"valence": "0.5", "energy": "0.4", "acousticness": "0.1", "tempo": "", "key": "-1"}, (None, -1)),
    ({"valence": 0.5, "energy": 0.4, "acousticness": 0.1, "tempo": 99.5, "key": 3.0}, (99.5, 3)),
])
def test_features_from_mapping(row, expect):
    from backend.features import features_from_mapping

    f = features_from_mapping(row)
    assert (f.tempo, f.key) == expect
