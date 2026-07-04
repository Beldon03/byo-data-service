from fastapi.testclient import TestClient


def test_index_serves_the_single_page_ui(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Bring Your Own Data" in response.text
