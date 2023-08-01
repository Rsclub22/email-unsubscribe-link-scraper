# Email Unsubscribe Links Scraper

This is a simple Python web application that logs into an email server, scans the inbox for unsubscribe links, and displays the results in a table. It also provides the option to move selected emails to the trash.

## Disclaimer
This App is still in development

## How to use

1. Clone this repository.
2. Build the Docker image:
docker build -t username/email-unsubscribe-links-scraper .
3. Run the Docker container:
docker run -p 5000:5000 -e EMAIL_USERNAME=your_email@example.com -e EMAIL_PASSWORD=your_email_password -e EMAIL_SERVER=your_email_server username/email-unsubscribe-links-scraper
4. Access the application in your web browser at http://localhost:5000.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
