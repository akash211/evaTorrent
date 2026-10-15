//! Keyless and keyed English subtitle providers (YifySubtitles & OpenSubtitles).

use regex::Regex;
use serde::{Deserialize, Serialize};

use crate::torrent::urlencoding_encode;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SubtitleResult {
    pub id: String,
    pub title: String,
    pub language: String,
    pub release: String,
    pub rating: f64,
    pub downloads: u64,
    pub provider: String,
    pub download_url: String,
    pub detail_url: String,
}

const YIFY_BASES: &[&str] = &["https://yifysubtitles.ch", "https://yifysubtitles.org"];

pub async fn search_yify(
    client: &reqwest::Client,
    query: &str,
    limit: usize,
) -> Vec<SubtitleResult> {
    let q = urlencoding_encode(query);
    let re_movie = Regex::new(r#"(?is)href="(/movie-imdb/tt\d+)"[^>]*>(.*?)</a>"#).unwrap();
    let re_sub = Regex::new(r#"(?is)href="(/subtitle/[^"]+\.zip)"[^>]*>(.*?)</a>"#).unwrap();
    let re_tags = Regex::new(r"<[^>]+>").unwrap();

    for &base in YIFY_BASES {
        let Ok(resp) = client.get(format!("{}/search?q={}", base, q)).send().await else {
            continue;
        };
        if !resp.status().is_success() {
            continue;
        }
        let Ok(html) = resp.text().await else {
            continue;
        };

        let mut movies = Vec::new();
        for cap in re_movie.captures_iter(&html) {
            let href = cap[1].to_string();
            let title = re_tags.replace_all(&cap[2], "").trim().to_string();
            if !movies.iter().any(|(h, _)| h == &href) {
                movies.push((
                    href,
                    if title.is_empty() {
                        query.to_string()
                    } else {
                        title
                    },
                ));
            }
            if movies.len() >= 5 {
                break;
            }
        }

        let mut results = Vec::new();
        for (href, title) in movies {
            let detail_url = format!("{}{}", base, href);
            let Ok(page) = client.get(&detail_url).send().await else {
                continue;
            };
            if !page.status().is_success() {
                continue;
            }
            let Ok(p_html) = page.text().await else {
                continue;
            };
            for sm in re_sub.captures_iter(&p_html) {
                let row_all = &sm[0];
                if !row_all.to_lowercase().contains("english") {
                    continue;
                }
                let dl = format!("{}{}", base, &sm[1]);
                let cell = re_tags.replace_all(&sm[2], " ").trim().to_string();
                results.push(SubtitleResult {
                    id: format!("yify:{}", urlencoding_encode(&dl)),
                    title: title.clone(),
                    language: "en".to_string(),
                    release: cell.chars().take(120).collect(),
                    rating: 0.0,
                    downloads: 0,
                    provider: "yify".to_string(),
                    download_url: dl,
                    detail_url: detail_url.clone(),
                });
                if results.len() >= limit {
                    return results;
                }
            }
        }
        if !results.is_empty() {
            return results;
        }
    }
    Vec::new()
}

pub async fn search_opensubtitles_org(
    client: &reqwest::Client,
    query: &str,
    limit: usize,
) -> Vec<SubtitleResult> {
    let url = format!(
        "https://www.opensubtitles.org/en/search2/sublanguageid-eng/moviename-{}",
        urlencoding_encode(query)
    );
    let Ok(resp) = client
        .get(&url)
        .header("Accept-Language", "en")
        .send()
        .await
    else {
        return Vec::new();
    };
    if !resp.status().is_success() {
        return Vec::new();
    }
    let Ok(html) = resp.text().await else {
        return Vec::new();
    };

    let re_row = Regex::new(r#"(?is)href="(/en/subtitles/(\d+)/[^"]*)"[^>]*>(.*?)</a>"#).unwrap();
    let re_tags = Regex::new(r"<[^>]+>").unwrap();

    let mut results = Vec::new();
    for cap in re_row.captures_iter(&html) {
        let detail = format!("https://www.opensubtitles.org{}", &cap[1]);
        let sub_id = &cap[2];
        let title = re_tags.replace_all(&cap[3], "").trim().to_string();
        let dl = format!("https://dl.opensubtitles.org/en/download/sub/{}", sub_id);
        results.push(SubtitleResult {
            id: format!("osorg:{}", sub_id),
            title: if title.is_empty() {
                query.to_string()
            } else {
                title.clone()
            },
            language: "en".to_string(),
            release: title.chars().take(120).collect(),
            rating: 0.0,
            downloads: 0,
            provider: "opensubtitles_org".to_string(),
            download_url: dl,
            detail_url: detail,
        });
        if results.len() >= limit {
            break;
        }
    }
    results
}
