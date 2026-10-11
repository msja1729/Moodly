"""Moodly's song recommender: songs that fit the user's taste, languages and mood.

Self-contained (it depends on Moodly only for the Spotify client, the feature
chain and the TrackTable it returns), so it can move to its own repository.

    engine.Recommender   background refresh -> a pool of songs (TrackTable)
    profile              favourite artists and top languages from listening
    language             which language a song is sung in
    sources              Spotify, Deezer, LRCLIB, iTunes, ReccoBeats
    store                SQLite cache of everything looked up
"""
from recommender.engine import Recommender, RecommenderParams

__all__ = ["Recommender", "RecommenderParams"]
