class CustomException(Exception):
    pass

# GitHub exceptions
class GitHubAPIError(CustomException):
    pass

class CommitNotFoundError(CustomException):
    pass

class GoogleCloudStorageError(CustomException):
    pass

class AnalyzerError(CustomException):
    pass

# GitLab exceptions
class GitLabAPIError(CustomException):
    pass