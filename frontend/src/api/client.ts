export const apiClient = {
  baseUrl: '/api/v1',
  get: async <T>(path: string): Promise<T> => {
    const response = await fetch(`${apiClient.baseUrl}${path}`);
    return response.json() as Promise<T>;
  },
};
