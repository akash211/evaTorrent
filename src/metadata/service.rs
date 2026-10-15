//! Free metadata providers for Discover (movies, TV, books, games) via YTS, TVMaze,
//! OpenLibrary, Steam, Wikipedia, and optional TMDB / OMDb API keys.

use std::collections::HashSet;
use std::time::Duration;

use futures::future::join_all;
use regex::Regex;
use serde_json::{json, Value};

use crate::torrent::urlencoding_encode;

pub fn keys_configured() -> Value {
    let tmdb = std::env::var("TMDB_API_KEY")
        .map(|s| !s.trim().is_empty())
        .unwrap_or(false);
    let omdb = std::env::var("OMDB_API_KEY")
        .map(|s| !s.trim().is_empty())
        .unwrap_or(false);
    json!({
        "tmdb": tmdb,
        "omdb": omdb,
    })
}

fn norm(text: &str) -> String {
    text.chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .map(|c| c.to_ascii_lowercase())
        .collect()
}

pub fn title_similarity(query: &str, title: &str) -> f64 {
    let nq = norm(query);
    let nt = norm(title);
    if nq.is_empty() || nt.is_empty() {
        return 0.0;
    }
    if nq == nt {
        return 1.0;
    }
    if nq.contains(&nt) || nt.contains(&nq) {
        return 0.9;
    }
    let common = nq.chars().filter(|c| nt.contains(*c)).count();
    (common as f64) / (nq.len().max(nt.len()) as f64)
}

fn clean_html(text: &str) -> String {
    let re = Regex::new(r"<[^>]+>").unwrap();
    let re_ws = Regex::new(r"\s+").unwrap();
    re_ws
        .replace_all(&re.replace_all(text, " "), " ")
        .trim()
        .to_string()
}

fn youtube_links(title: &str, year: Option<i64>, trailer_code: Option<&str>) -> (String, String) {
    if let Some(code) = trailer_code.filter(|s| !s.is_empty()) {
        return (
            format!("https://www.youtube.com/watch?v={}", code),
            format!("https://www.youtube.com/embed/{}", code),
        );
    }
    let q = match year {
        Some(y) => format!("{} {} official trailer", title, y),
        None => format!("{} official trailer", title),
    };
    let enc = urlencoding_encode(&q);
    (
        format!("https://www.youtube.com/results?search_query={}", enc),
        format!("https://www.youtube.com/embed?listType=search&list={}", enc),
    )
}

fn justwatch_url(title: &str) -> String {
    format!(
        "https://www.justwatch.com/in/search?q={}",
        urlencoding_encode(title)
    )
}

async fn get_json(client: &reqwest::Client, url: &str) -> Option<Value> {
    let resp = client.get(url).send().await.ok()?;
    if resp.status().is_success() {
        resp.json::<Value>().await.ok()
    } else {
        None
    }
}

async fn search_yts(client: &reqwest::Client, query: &str, limit: usize) -> Vec<Value> {
    let url = format!(
        "https://yts.mx/api/v2/list_movies.json?query_term={}&limit={}&sort_by=like_count&order_by=desc",
        urlencoding_encode(query),
        limit.min(20)
    );
    let Some(data) = get_json(client, &url).await else {
        return Vec::new();
    };
    let Some(movies) = data
        .get("data")
        .and_then(|d| d.get("movies"))
        .and_then(|m| m.as_array())
    else {
        return Vec::new();
    };

    let mut out = Vec::new();
    for m in movies.iter().take(limit) {
        let title = m
            .get("title_english")
            .or_else(|| m.get("title"))
            .and_then(|v| v.as_str())
            .unwrap_or(query);
        let year = m.get("year").and_then(|v| v.as_i64());
        let imdb_code = m.get("imdb_code").and_then(|v| v.as_str()).unwrap_or("");
        let yt_code = m.get("yt_trailer_code").and_then(|v| v.as_str());
        let (yt_url, yt_embed) = youtube_links(title, year, yt_code);
        let runtime = m.get("runtime").and_then(|v| v.as_i64());

        out.push(json!({
            "type": "movie",
            "title": title,
            "year": year,
            "release_date": year.map(|y| y.to_string()),
            "runtime_mins": runtime,
            "runtime": runtime.map(|r| format!("{} min", r)),
            "genres": m.get("genres").cloned().unwrap_or_else(|| json!([])),
            "languages": ["English"],
            "overview": m.get("summary").or_else(|| m.get("description_full")).and_then(|v| v.as_str()).unwrap_or(""),
            "poster_url": m.get("large_cover_image").or_else(|| m.get("medium_cover_image")),
            "imdb_id": imdb_code,
            "imdb_url": if !imdb_code.is_empty() {
                format!("https://www.imdb.com/title/{}/", imdb_code)
            } else {
                format!("https://www.imdb.com/find?q={}", urlencoding_encode(title))
            },
            "imdb_rating": m.get("rating"),
            "rotten_tomatoes": null,
            "rotten_tomatoes_url": format!("https://www.rottentomatoes.com/search?search={}", urlencoding_encode(title)),
            "wiki_url": format!("https://en.wikipedia.org/wiki/Special:Search?search={}", urlencoding_encode(title)),
            "budget": null,
            "revenue_box_office": null,
            "ott_india": [],
            "justwatch_in_url": justwatch_url(title),
            "youtube_trailer_url": yt_url,
            "youtube_trailer_embed": yt_embed,
            "provider": "YTS",
            "source_url": m.get("url").and_then(|v| v.as_str()).unwrap_or("https://yts.mx"),
        }));
    }
    out
}

async fn search_tvmaze(client: &reqwest::Client, query: &str, limit: usize) -> Vec<Value> {
    let url = format!(
        "https://api.tvmaze.com/search/shows?q={}",
        urlencoding_encode(query)
    );
    let Some(data) = get_json(client, &url).await else {
        return Vec::new();
    };
    let Some(entries) = data.as_array() else {
        return Vec::new();
    };

    let mut out = Vec::new();
    for entry in entries.iter().take(limit) {
        let Some(s) = entry.get("show") else { continue };
        let title = s.get("name").and_then(|v| v.as_str()).unwrap_or(query);
        let premiered = s.get("premiered").and_then(|v| v.as_str());
        let year = premiered
            .and_then(|p| p.get(0..4))
            .and_then(|y| y.parse::<i64>().ok());
        let ended = s.get("ended").and_then(|v| v.as_str());
        let year_end = ended
            .and_then(|p| p.get(0..4))
            .and_then(|y| y.parse::<i64>().ok());
        let runtime = s.get("runtime").and_then(|v| v.as_i64());
        let rating = s
            .get("rating")
            .and_then(|r| r.get("average"))
            .and_then(|v| v.as_f64());
        let network = s
            .get("network")
            .and_then(|n| n.get("name"))
            .or_else(|| s.get("webChannel").and_then(|w| w.get("name")))
            .and_then(|v| v.as_str());
        let imdb_id = s
            .get("externals")
            .and_then(|e| e.get("imdb"))
            .and_then(|v| v.as_str());
        let (yt_url, yt_embed) = youtube_links(title, year, None);

        out.push(json!({
            "type": "tv",
            "title": title,
            "year": year,
            "year_end": year_end,
            "release_date": premiered,
            "runtime_mins": runtime,
            "runtime": runtime.map(|r| format!("{} min/ep", r)),
            "genres": s.get("genres").cloned().unwrap_or_else(|| json!([])),
            "languages": s.get("language").and_then(|v| v.as_str()).map(|l| vec![l]).unwrap_or_default(),
            "overview": clean_html(s.get("summary").and_then(|v| v.as_str()).unwrap_or("")),
            "poster_url": s.get("image").and_then(|i| i.get("medium").or_else(|| i.get("original"))),
            "imdb_id": imdb_id,
            "imdb_url": match imdb_id {
                Some(id) if !id.is_empty() => format!("https://www.imdb.com/title/{}/", id),
                _ => format!("https://www.imdb.com/find?q={}", urlencoding_encode(title)),
            },
            "imdb_rating": rating,
            "rotten_tomatoes": null,
            "rotten_tomatoes_url": format!("https://www.rottentomatoes.com/search?search={}", urlencoding_encode(title)),
            "wiki_url": format!("https://en.wikipedia.org/wiki/Special:Search?search={}", urlencoding_encode(title)),
            "budget": null,
            "revenue_box_office": null,
            "ott_india": network.map(|n| vec![n]).unwrap_or_default(),
            "network": network,
            "justwatch_in_url": justwatch_url(title),
            "youtube_trailer_url": yt_url,
            "youtube_trailer_embed": yt_embed,
            "provider": "TVMaze",
            "source_url": s.get("url").and_then(|v| v.as_str()).unwrap_or("https://www.tvmaze.com"),
        }));
    }
    out
}

async fn search_openlibrary(client: &reqwest::Client, query: &str, limit: usize) -> Vec<Value> {
    let url = format!(
        "https://openlibrary.org/search.json?q={}&limit={}",
        urlencoding_encode(query),
        limit
    );
    let Some(data) = get_json(client, &url).await else {
        return Vec::new();
    };
    let Some(docs) = data.get("docs").and_then(|v| v.as_array()) else {
        return Vec::new();
    };

    let mut out = Vec::new();
    for d in docs.iter().take(limit) {
        let title = d.get("title").and_then(|v| v.as_str()).unwrap_or(query);
        let year = d.get("first_publish_year").and_then(|v| v.as_i64());
        let cover_id = d.get("cover_i").and_then(|v| v.as_i64());
        let pages = d.get("number_of_pages_median").and_then(|v| v.as_i64());
        let key = d.get("key").and_then(|v| v.as_str()).unwrap_or("");
        let (yt_url, yt_embed) = youtube_links(title, year, None);

        out.push(json!({
            "type": "book",
            "title": title,
            "year": year,
            "release_date": year.map(|y| y.to_string()),
            "runtime_mins": null,
            "runtime": pages.map(|p| format!("{} pages", p)),
            "genres": [],
            "languages": d.get("language").cloned().unwrap_or_else(|| json!([])),
            "overview": "",
            "poster_url": cover_id.map(|id| format!("https://covers.openlibrary.org/b/id/{}-L.jpg", id)),
            "imdb_id": null,
            "imdb_url": null,
            "imdb_rating": null,
            "rotten_tomatoes": null,
            "rotten_tomatoes_url": null,
            "wiki_url": format!("https://en.wikipedia.org/wiki/Special:Search?search={}", urlencoding_encode(title)),
            "budget": null,
            "revenue_box_office": null,
            "ott_india": [],
            "youtube_trailer_url": yt_url,
            "youtube_trailer_embed": yt_embed,
            "provider": "OpenLibrary",
            "source_url": format!("https://openlibrary.org{}", key),
        }));
    }
    out
}

async fn search_steam(client: &reqwest::Client, query: &str, limit: usize) -> Vec<Value> {
    let url = format!(
        "https://store.steampowered.com/api/storesearch/?term={}&l=english&cc=IN",
        urlencoding_encode(query)
    );
    let Some(data) = get_json(client, &url).await else {
        return Vec::new();
    };
    let Some(items) = data.get("items").and_then(|v| v.as_array()) else {
        return Vec::new();
    };

    let mut out = Vec::new();
    for it in items.iter().take(limit) {
        let appid = it.get("id").and_then(|v| v.as_i64());
        let name = it.get("name").and_then(|v| v.as_str()).unwrap_or(query);
        let (yt_url, yt_embed) = youtube_links(&format!("{} gameplay", name), None, None);

        out.push(json!({
            "type": "game",
            "title": name,
            "year": null,
            "release_date": null,
            "runtime_mins": null,
            "runtime": null,
            "genres": [],
            "languages": [],
            "overview": "",
            "poster_url": it.get("tiny_image"),
            "imdb_id": null,
            "imdb_url": null,
            "imdb_rating": it.get("metascore"),
            "metascore": it.get("metascore"),
            "rotten_tomatoes": null,
            "rotten_tomatoes_url": null,
            "wiki_url": format!("https://en.wikipedia.org/wiki/Special:Search?search={}", urlencoding_encode(name)),
            "budget": null,
            "revenue_box_office": null,
            "ott_india": [],
            "steam_appid": appid,
            "youtube_trailer_url": yt_url,
            "youtube_trailer_embed": yt_embed,
            "provider": "Steam",
            "source_url": appid.map(|id| format!("https://store.steampowered.com/app/{}/", id)).unwrap_or_else(|| "https://store.steampowered.com".to_string()),
        }));
    }
    out
}

pub fn apply_year_filter(results: Vec<Value>, year: Option<i64>) -> (Vec<Value>, bool) {
    let Some(target_year) = year else {
        return (results, false);
    };
    let mut kept = Vec::new();
    for r in results {
        let Some(y) = r.get("year").and_then(|v| v.as_i64()) else {
            continue;
        };
        if r.get("type").and_then(|v| v.as_str()) == Some("tv") {
            let y_end = r.get("year_end").and_then(|v| v.as_i64());
            match y_end {
                None if target_year >= y => kept.push(r),
                Some(end) if target_year >= y && target_year <= end => kept.push(r),
                _ => {}
            }
        } else if y == target_year {
            kept.push(r);
        }
    }
    (kept, true)
}

pub async fn lookup_media(
    query: &str,
    media_type: &str,
    limit: usize,
    timeout_secs: f64,
    year: Option<i64>,
) -> Value {
    let q = query.trim();
    let keys = keys_configured();
    let mut mt = media_type.trim().to_lowercase();
    if mt == "series" || mt == "show" || mt == "shows" {
        mt = "tv".to_string();
    }
    if !["movie", "tv", "book", "game", "all"].contains(&mt.as_str()) {
        mt = "all".to_string();
    }

    if q.is_empty() {
        return json!({
            "query": q,
            "type": mt,
            "total_found": 0,
            "results": [],
            "keys_configured": keys,
        });
    }

    let Ok(client) = reqwest::Client::builder()
        .timeout(Duration::from_secs_f64(timeout_secs.max(5.0)))
        .user_agent("evaTorrent/1.0.0 (Discover metadata lookup)")
        .build()
    else {
        return json!({
            "query": q,
            "type": mt,
            "total_found": 0,
            "results": [],
            "keys_configured": keys,
        });
    };

    let mut futs: Vec<futures::future::BoxFuture<'_, Vec<Value>>> = Vec::new();
    if mt == "movie" || mt == "all" {
        futs.push(Box::pin(search_yts(&client, q, 6)));
    }
    if mt == "tv" || mt == "all" {
        futs.push(Box::pin(search_tvmaze(&client, q, 6)));
    }
    if mt == "book" || mt == "all" {
        futs.push(Box::pin(search_openlibrary(&client, q, 6)));
    }
    if mt == "game" || mt == "all" {
        futs.push(Box::pin(search_steam(&client, q, 6)));
    }

    let lists = join_all(futs).await;
    let mut seen = HashSet::new();
    let mut deduped = Vec::new();
    for list in lists {
        for item in list {
            let t = item
                .get("type")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let title = item
                .get("title")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_lowercase();
            if seen.insert((t, title)) {
                deduped.push(item);
            }
        }
    }

    deduped.sort_by(|a, b| {
        let sa = title_similarity(q, a.get("title").and_then(|v| v.as_str()).unwrap_or(""));
        let sb = title_similarity(q, b.get("title").and_then(|v| v.as_str()).unwrap_or(""));
        sb.partial_cmp(&sa).unwrap_or(std::cmp::Ordering::Equal)
    });

    let (filtered, year_applied) = apply_year_filter(deduped, year);
    let total_found = filtered.len();
    let results: Vec<Value> = filtered.into_iter().take(limit).collect();

    json!({
        "query": q,
        "type": mt,
        "year": year,
        "year_filter_applied": year_applied,
        "total_found": total_found,
        "returned": results.len(),
        "keys_configured": keys,
        "results": results,
    })
}
